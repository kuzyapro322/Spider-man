"""Прикладной сервис: связывает BPMN, агрегаты, платежи, замок и таймеры."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol

from domain import (
    Account, Active, BatteryLevel, BookingExpired, ClockMovedBack,
    FinishedUnpaid, GatewayUnresolved, InvariantViolation, InvalidValueObject,
    NotFound, Paid, Paused, PreauthorizationPending, Preparing, Registered,
    Rejected, Reserved, Scooter, ScooterId, Tariff, TransitionNotAllowed,
    Trip, WaitingPhoto, WithDebt,
)


class Clock(Protocol):
    def now(self) -> datetime: ...


class PaymentPort(Protocol):
    def request_preauthorization(self, trip_id: str, amount: int) -> str: ...
    def request_payment(self, trip_id: str, amount: float) -> str: ...


class LockPort(Protocol):
    def unlock(self, scooter_id: str) -> bool: ...
    def lock(self, scooter_id: str) -> None: ...


class ScooterRepository(Protocol):
    def get(self, scooter_id: str) -> Scooter: ...
    def save(self, scooter: Scooter) -> None: ...


class TripRepository(Protocol):
    def get(self, trip_id: str) -> Trip: ...
    def save(self, trip: Trip) -> None: ...


class InMemoryRepository:
    """Простой репозиторий для лабораторной: объекты хранятся в словаре."""
    def __init__(self):
        self.items = {}

    def get(self, key):
        if key not in self.items:
            raise NotFound(f"Объект {key} не найден")
        return self.items[key]

    def save(self, key, value):
        self.items[key] = value


class ShiftedClock:
    """Часы реального времени со сдвигом для проверки таймеров."""
    def __init__(self):
        self._shift = timedelta(0)

    def now(self):
        # datetime.now() допустим именно здесь, в адаптере часов.
        return datetime.now(timezone.utc).replace(tzinfo=None) + self._shift

    def move_to(self, target):
        if target.tzinfo is not None:
            target = target.astimezone(timezone.utc).replace(tzinfo=None)
        current = self.now()
        if target < current:
            raise ClockMovedBack("Нельзя переводить время назад")
        self._shift += target - current
        return self.now()

    def advance_minutes(self, minutes):
        if type(minutes) not in (int, float) or not 0 <= minutes <= 100000:
            raise InvalidValueObject("minutes должно быть неотрицательным числом")
        self._shift += timedelta(minutes=minutes)
        return self.now()


class FakePayment:
    """Вместо настоящего банка возвращает ID тестового запроса."""
    def request_preauthorization(self, trip_id, amount):
        return f"PREAUTH-{trip_id}-{amount}"

    def request_payment(self, trip_id, amount):
        return f"PAY-{trip_id}-{amount:.2f}"


class FakeLock:
    """Вместо настоящего замка запоминает, что команда выполнена."""
    def __init__(self):
        self.unlocked = set()

    def unlock(self, scooter_id):
        self.unlocked.add(scooter_id)
        return True

    def lock(self, scooter_id):
        self.unlocked.discard(scooter_id)


def xor_gateway(**conditions):
    """У XOR должна выполниться ровно одна ветвь."""
    if not conditions or any(type(x) is not bool for x in conditions.values()):
        raise GatewayUnresolved("Неверные условия XOR")
    matched = [key for key, value in conditions.items() if value]
    if len(matched) != 1:
        raise GatewayUnresolved("XOR не определил одну ветвь")
    return matched[0]


class TripService:
    def __init__(self, clock=None):
        self.clock = clock or ShiftedClock()
        self.scooters = InMemoryRepository()
        self.trips = InMemoryRepository()
        self.accounts = InMemoryRepository()
        self.payment = FakePayment()
        self.lock = FakeLock()

    def add_scooter(self, scooter_id, battery):
        if scooter_id in self.scooters.items:
            raise InvariantViolation("Самокат уже существует")
        scooter = Scooter(ScooterId(scooter_id), BatteryLevel(battery))
        self.scooters.save(scooter_id, scooter)
        return scooter

    def get_account(self, user_id):
        if user_id not in self.accounts.items:
            self.accounts.save(user_id, Account(user_id))
        return self.accounts.get(user_id)

    def check_client_debt(self, user_id):
        return self.get_account(user_id).blocked

    def check_parking_zone(self, parking_allowed):
        if type(parking_allowed) is not bool:
            raise InvalidValueObject("parking_allowed должен быть true/false")
        xor_gateway(allowed=parking_allowed, forbidden=not parking_allowed)
        return parking_allowed

    def add_demo_debt(self, user_id, amount):
        account = self.get_account(user_id)
        account.add_debt("demo-debt", amount)
        return account

    def register_trip(self, trip_id, scooter_id, user_id, tariff=None):
        if trip_id in self.trips.items:
            raise InvariantViolation("Поездка с таким ID существует")
        self.scooters.get(scooter_id)
        self.get_account(user_id)
        trip = Trip(trip_id, ScooterId(scooter_id), user_id, tariff or Tariff())
        self.trips.save(trip_id, trip)
        trip._record("RequestReceived", self.clock.now())
        return trip

    def reserve(self, trip_id):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, Registered):
            raise TransitionNotAllowed(trip.state, "reserve")
        scooter = self.scooters.get(trip.scooter_id.value)
        scooter.reserve(trip.id)
        trip.reserve(self.clock.now())
        return trip

    def request_start(self, trip_id):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, Reserved):
            raise TransitionNotAllowed(trip.state, "request_start")
        has_debt = self.check_client_debt(trip.user_id)
        xor_gateway(no_debt=not has_debt, has_debt=has_debt)  # G1/G2
        trip.request_start(self.clock.now(), has_debt)
        if isinstance(trip._state, Rejected):
            self.scooters.get(trip.scooter_id.value).release(trip.id)
        else:
            ref = self.payment.request_preauthorization(trip.id, 300)
            trip.preauth_request_id = ref
            trip._record("PaymentGatewayRequested", self.clock.now(), ref)
        return trip

    def preauth_result(self, trip_id, success):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, PreauthorizationPending):
            raise TransitionNotAllowed(trip.state, "receive_preauthorization")
        if type(success) is not bool:
            raise InvalidValueObject("success должен быть true/false")
        xor_gateway(success=success, rejected=not success)  # G3/G4
        trip.receive_preauthorization(self.clock.now(), success)
        if isinstance(trip._state, Rejected):
            self.scooters.get(trip.scooter_id.value).release(trip.id)
        return trip

    def unlock(self, trip_id):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, Preparing):
            raise TransitionNotAllowed(trip.state, "unlock")
        trip._check_booking(self.clock.now())
        if trip.lock_unlocked:
            raise InvariantViolation("Замок уже открыт")
        if not self.lock.unlock(trip.scooter_id.value):
            raise InvariantViolation("Замок не подтвердил команду")
        trip.confirm_unlock(self.clock.now())
        return trip

    def start_billing(self, trip_id):
        trip = self.trips.get(trip_id)
        trip.confirm_billing(self.clock.now())
        return trip

    def start_ride(self, trip_id):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, Preparing):
            raise TransitionNotAllowed(trip.state, "start_ride")
        ready = trip.lock_unlocked and trip.billing_started
        xor_gateway(ready=ready, not_ready=not ready)  # G6
        if not ready:
            raise InvariantViolation("Подготовка не завершена")
        scooter = self.scooters.get(trip.scooter_id.value)
        scooter.can_start_rental(trip.id)
        trip.start_ride(self.clock.now())
        scooter.start_rental(trip.id)
        return trip

    def prepare(self, trip_id):
        # Две ветви BPMN выполняем последовательно, затем AND-слияние.
        self.unlock(trip_id)
        self.start_billing(trip_id)
        return self.start_ride(trip_id)

    def pause(self, trip_id):
        trip = self.trips.get(trip_id)
        trip.pause(self.clock.now())
        return trip

    def resume(self, trip_id):
        trip = self.trips.get(trip_id)
        trip.resume(self.clock.now())
        return trip

    def request_finish(self, trip_id, parking_allowed):
        trip = self.trips.get(trip_id)
        # Сначала проверяем допустимость события (409), затем зону (G7/G11).
        if not isinstance(trip._state, (Active, Paused)):
            raise TransitionNotAllowed(trip.state, "request_finish")
        allowed = self.check_parking_zone(parking_allowed)
        trip.request_finish(self.clock.now(), allowed)  # G7/G11
        return trip

    def receive_photo(self, trip_id, photo_url):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, WaitingPhoto):
            raise TransitionNotAllowed(trip.state, "receive_photo")
        if not isinstance(photo_url, str) or not photo_url.strip():
            raise InvalidValueObject("Нужна фотография")
        trip.receive_photo(self.clock.now(), photo_url)  # G8
        self.scooters.get(trip.scooter_id.value).release(trip.id)
        self.lock.lock(trip.scooter_id.value)
        ref = self.payment.request_payment(trip.id, trip.cost)
        trip.payment_request_id = ref
        trip._record("PaymentGatewayRequested", self.clock.now(), ref)
        return trip

    def payment_result(self, trip_id, success):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, FinishedUnpaid):
            raise TransitionNotAllowed(trip.state, "receive_payment")
        if type(success) is not bool:
            raise InvalidValueObject("success должен быть true/false")
        xor_gateway(paid=success, debt=not success)  # G9/G10
        if not success and trip.cost <= 0:
            raise InvariantViolation("Нельзя начислить нулевой долг")
        trip.receive_payment(self.clock.now(), success)
        if isinstance(trip._state, WithDebt):
            self.get_account(trip.user_id).add_debt(trip.id, trip.cost)
            trip._record("AccountBlockedAndNotified", self.clock.now())
        return trip

    def operator_contact(self, trip_id, result):
        trip = self.trips.get(trip_id)
        trip.record_operator_contact(self.clock.now(), result)
        return trip

    def on_expire_booking(self, trip_id, now):
        trip = self.trips.get(trip_id)
        if not isinstance(trip._state, (Reserved, PreauthorizationPending, Preparing)):
            raise TransitionNotAllowed(trip.state, "expire_booking")
        if trip.lock_unlocked:
            self.lock.lock(trip.scooter_id.value)
        trip.expire_booking(now)
        self.scooters.get(trip.scooter_id.value).release(trip.id)
        trip.lock_unlocked = False
        trip.billing_started = False
        return trip

    def on_ride_timeout(self, trip_id, now):
        trip = self.trips.get(trip_id)
        trip.ride_timeout(now)
        return trip

    def tick(self, advance_minutes=None, at=None):
        if advance_minutes is not None and at is not None:
            raise InvalidValueObject("Выбери одно: advance_minutes или now")
        if advance_minutes is not None:
            now = self.clock.advance_minutes(advance_minutes)
        elif at is not None:
            now = self.clock.move_to(at)
        else:
            now = self.clock.now()
        events = []
        for trip in self.trips.items.values():
            if (isinstance(trip._state, (Reserved, PreauthorizationPending, Preparing))
                    and now >= trip.booking_deadline):
                self.on_expire_booking(trip.id, now)
                events.append({"trip_id": trip.id, "event": "expire_booking"})
            elif (isinstance(trip._state, (Active, Paused, WaitingPhoto))
                    and now > trip.started_at + timedelta(hours=12)
                    and not trip.operator_notified):
                self.on_ride_timeout(trip.id, now)
                events.append({"trip_id": trip.id, "event": "ride_timeout"})
        return {"now": now.isoformat(), "fired": events}
