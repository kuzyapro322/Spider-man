"""Доменная модель: сущности, инварианты, 12 классов State и события поездки."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from math import isfinite


class DomainError(Exception):
    code = "domain_error"


class InvalidValueObject(DomainError):
    code = "invalid_value"


class InvariantViolation(DomainError):
    code = "invariant_violation"


class TransitionNotAllowed(DomainError):
    code = "transition_not_allowed"

    def __init__(self, state, event):
        super().__init__(f"Событие {event} запрещено в состоянии «{state}»")


class NotFound(DomainError):
    code = "not_found"


class GatewayUnresolved(DomainError):
    code = "gateway_unresolved"


class ClockMovedBack(DomainError):
    code = "clock_moved_back"


@dataclass(frozen=True)
class ScooterId:
    value: str

    def __post_init__(self):
        if not isinstance(self.value, str) or not self.value.strip():
            raise InvalidValueObject("ID самоката не может быть пустым")


@dataclass(frozen=True)
class BatteryLevel:
    value: int

    def __post_init__(self):
        if type(self.value) is not int or not 0 <= self.value <= 100:
            raise InvalidValueObject("Заряд должен быть целым числом от 0 до 100")


@dataclass(frozen=True)
class Tariff:
    price_per_minute: float = 5.0
    minimum_price: float = 50.0
    pause_price_per_minute: float = 1.0

    def __post_init__(self):
        values = (self.price_per_minute, self.minimum_price, self.pause_price_per_minute)
        if any(type(v) not in (int, float) or not isfinite(v) for v in values):
            raise InvalidValueObject("Тариф должен содержать числа")
        if not (self.price_per_minute > 0 and self.minimum_price >= 0
                and 0 <= self.pause_price_per_minute < self.price_per_minute):
            raise InvalidValueObject("Неверные значения тарифа")


@dataclass(frozen=True)
class TripPeriod:
    started_at: datetime
    ended_at: datetime

    def __post_init__(self):
        if self.ended_at < self.started_at:
            raise InvalidValueObject("Конец периода раньше начала")


@dataclass(frozen=True)
class DomainEvent:
    name: str
    at: datetime
    detail: str = ""

    def to_dict(self):
        return {"name": self.name, "at": self.at.isoformat(), "detail": self.detail}


class Scooter:
    AVAILABLE = "свободен"
    RESERVED = "забронирован"
    RENTED = "арендован"
    MAINTENANCE = "на обслуживании"

    def __init__(self, scooter_id: ScooterId, battery: BatteryLevel):
        self._id = scooter_id
        self._battery = battery
        self._status = self.AVAILABLE
        self._trip_id = None

    @property
    def id(self):
        return self._id

    @property
    def status(self):
        return self._status

    def reserve(self, trip_id, minimum_battery=20):
        if self._status != self.AVAILABLE:
            raise InvariantViolation("Самокат уже занят")
        if self._battery.value < minimum_battery:
            raise InvariantViolation("Недостаточный заряд")
        self._trip_id = trip_id
        self._status = self.RESERVED

    def can_start_rental(self, trip_id, minimum_battery=20):
        if self._status != self.RESERVED or self._trip_id != trip_id:
            raise InvariantViolation("Нет брони этого самоката для этой поездки")
        if self._battery.value < minimum_battery:
            raise InvariantViolation("Недостаточный заряд для начала аренды")

    def start_rental(self, trip_id):
        self.can_start_rental(trip_id)
        self._status = self.RENTED

    def release(self, trip_id):
        if self._trip_id != trip_id or self._status not in (self.RESERVED, self.RENTED):
            raise InvariantViolation("Нельзя освободить чужой самокат")
        self._trip_id = None
        self._status = self.AVAILABLE

    def to_dict(self):
        return {"id": self.id.value, "battery": self._battery.value,
                "status": self.status, "trip_id": self._trip_id}


class Account:
    def __init__(self, user_id):
        if not isinstance(user_id, str) or not user_id.strip():
            raise InvalidValueObject("Нужен ID клиента")
        self.user_id = user_id
        self._debts = {}

    @property
    def debt(self):
        return round(sum(self._debts.values()), 2)

    @property
    def blocked(self):
        return self.debt > 0

    def add_debt(self, reason, amount):
        if type(amount) not in (int, float) or not isfinite(amount) or amount <= 0:
            raise InvalidValueObject("Долг должен быть положительным числом")
        self._debts.setdefault(reason, amount)  # второй раз долг не начисляем

    def to_dict(self):
        return {"user_id": self.user_id, "debt": self.debt, "blocked": self.blocked}


class TripState:
    """Базовое состояние запрещает все события; дочерние классы разрешают нужные."""
    name = "Неизвестно"
    allowed_events = ()

    def _no(self, event):
        raise TransitionNotAllowed(self.name, event)

    def reserve(self, trip, now): self._no("reserve")
    def request_start(self, trip, now, has_debt): self._no("request_start")
    def receive_preauthorization(self, trip, now, success): self._no("receive_preauthorization")
    def expire_booking(self, trip, now): self._no("expire_booking")
    def start_ride(self, trip, now): self._no("start_ride")
    def pause(self, trip, now): self._no("pause")
    def resume(self, trip, now): self._no("resume")
    def request_finish(self, trip, now, parking_allowed): self._no("request_finish")
    def receive_photo(self, trip, now, photo_url): self._no("receive_photo")
    def receive_payment(self, trip, now, success): self._no("receive_payment")
    def ride_timeout(self, trip, now): self._no("ride_timeout")


class Registered(TripState):
    name = "Зарегистрирована"
    allowed_events = ("reserve",)

    def reserve(self, trip, now):
        trip.booked_at = now
        trip.booking_deadline = now + timedelta(minutes=10)
        trip._record("ReservationCreated", now)
        return Reserved()


class Reserved(TripState):
    name = "Забронирована"
    allowed_events = ("request_start", "expire_booking")

    def request_start(self, trip, now, has_debt):
        trip._check_booking(now)
        if type(has_debt) is not bool:
            raise InvalidValueObject("has_debt должен быть true/false")
        if has_debt:  # G2
            trip._record("RejectedForDebt", now)
            return Rejected()
        trip._record("PreauthorizationRequested", now, "300 RUB")  # G1
        return PreauthorizationPending()

    def expire_booking(self, trip, now):
        return trip._expire(now)


class PreauthorizationPending(TripState):
    name = "Направлена на предавторизацию"
    allowed_events = ("receive_preauthorization", "expire_booking")

    def receive_preauthorization(self, trip, now, success):
        trip._check_booking(now)
        if type(success) is not bool:
            raise InvalidValueObject("success должен быть true/false")
        if not success:  # G4
            trip._record("PreauthorizationRejected", now)
            return Rejected()
        trip._record("UnlockRequested", now)       # G3: начало AND-развилки
        trip._record("BillingStartRequested", now)
        return Preparing()

    def expire_booking(self, trip, now):
        return trip._expire(now)


class Preparing(TripState):
    name = "Подготовка к запуску"
    allowed_events = ("start_ride", "expire_booking")

    def start_ride(self, trip, now):
        trip._check_booking(now)
        if not (trip.lock_unlocked and trip.billing_started):  # G6
            raise InvariantViolation("Нужны разблокировка И запуск тарификации")
        trip.started_at = now
        trip._mode = "normal"
        trip._segment_started = now
        trip._record("RideStarted", now)
        return Active()

    def expire_booking(self, trip, now):
        return trip._expire(now)


class Active(TripState):
    name = "Начата"
    allowed_events = ("pause", "request_finish", "ride_timeout")

    def pause(self, trip, now):
        trip._change_rate(now, "pause")
        trip._record("PauseStarted", now)
        return Paused()

    def request_finish(self, trip, now, parking_allowed):
        return trip._finish_request(now, parking_allowed, self)

    def ride_timeout(self, trip, now):
        return trip._long_ride(now, self)


class Paused(TripState):
    name = "Приостановлена"
    allowed_events = ("resume", "request_finish", "ride_timeout")

    def resume(self, trip, now):
        trip._change_rate(now, "normal")
        trip._record("RideResumed", now)
        return Active()

    def request_finish(self, trip, now, parking_allowed):
        return trip._finish_request(now, parking_allowed, self)

    def ride_timeout(self, trip, now):
        return trip._long_ride(now, self)


class WaitingPhoto(TripState):
    name = "Подготовлена к завершению"
    allowed_events = ("receive_photo", "ride_timeout")

    def receive_photo(self, trip, now, photo_url):
        if not isinstance(photo_url, str) or not photo_url.strip():  # G8
            raise InvalidValueObject("Нужна фотография")
        if not trip.parking_allowed:
            raise InvariantViolation("Парковка не была подтверждена")
        trip._check_started(now)
        trip.photo_url = photo_url
        trip.finished_at = now
        trip._close_segment(now)
        trip.cost = trip.calculate_cost()
        trip._record("BillingStopped", now)
        trip._record("PaymentRequested", now, str(trip.cost))
        return FinishedUnpaid()

    def ride_timeout(self, trip, now):
        return trip._long_ride(now, self)


class FinishedUnpaid(TripState):
    name = "Завершена, не оплачена"
    allowed_events = ("receive_payment",)

    def receive_payment(self, trip, now, success):
        if type(success) is not bool:
            raise InvalidValueObject("success должен быть true/false")
        if success:  # G9
            trip._record("PaymentSucceeded", now)
            return Paid()
        trip._record("PaymentFailed", now)  # G10
        return WithDebt()


class Paid(TripState):
    name = "Завершена и оплачена"


class WithDebt(TripState):
    name = "Завершена с задолженностью"


class Rejected(TripState):
    name = "Отклонена до начала"


class BookingExpired(TripState):
    name = "Отменена по истечении брони"


class Trip:
    """Корень агрегата Trip. Менять состояние можно только методами событий."""
    def __init__(self, trip_id, scooter_id: ScooterId, user_id, tariff: Tariff):
        if not isinstance(trip_id, str) or not trip_id.strip():
            raise InvalidValueObject("Нужен ID поездки")
        if not isinstance(user_id, str) or not user_id.strip():
            raise InvalidValueObject("Нужен ID клиента")
        self.id = trip_id
        self.scooter_id = scooter_id  # ссылка по ID, не объект Scooter!
        self.user_id = user_id
        self.tariff = tariff
        self._state = Registered()
        self.booked_at = self.booking_deadline = None
        self.started_at = self.finished_at = None
        self.lock_unlocked = self.billing_started = False
        self.parking_allowed = False
        self.photo_url = None
        self.preauth_request_id = None
        self.payment_request_id = None
        self.cost = None
        self.operator_notified = False
        self.operator_contact = None
        self._mode = self._segment_started = None
        self._segments = []
        self._journal = []
        self._last_at = None

    @property
    def state(self):
        return self._state.name

    @property
    def allowed_events(self):
        return [e for e in self._state.allowed_events
                if e != "ride_timeout" or not self.operator_notified]

    @property
    def journal(self):
        return tuple(self._journal)

    def _record(self, name, now, detail=""):
        self._journal.append(DomainEvent(name, now, detail))

    def _apply(self, event, now, *args):
        if self._last_at is not None and now < self._last_at:
            raise InvariantViolation("Время события нельзя переводить назад")
        before = self.state
        new_state = getattr(self._state, event)(self, now, *args)
        self._state = new_state
        self._last_at = now
        self._record("Transition", now, f"{before} --{event}--> {self.state}")

    def reserve(self, now): self._apply("reserve", now)
    def request_start(self, now, has_debt): self._apply("request_start", now, has_debt)
    def receive_preauthorization(self, now, success): self._apply("receive_preauthorization", now, success)
    def expire_booking(self, now): self._apply("expire_booking", now)
    def start_ride(self, now): self._apply("start_ride", now)
    def pause(self, now): self._apply("pause", now)
    def resume(self, now): self._apply("resume", now)
    def request_finish(self, now, parking_allowed): self._apply("request_finish", now, parking_allowed)
    def receive_photo(self, now, photo_url): self._apply("receive_photo", now, photo_url)
    def receive_payment(self, now, success): self._apply("receive_payment", now, success)
    def ride_timeout(self, now): self._apply("ride_timeout", now)

    def confirm_unlock(self, now):
        if not isinstance(self._state, Preparing):
            raise TransitionNotAllowed(self.state, "confirm_unlock")
        self._check_booking(now)
        if self.lock_unlocked:
            raise InvariantViolation("Замок уже разблокирован")
        self.lock_unlocked = True
        self._record("LockUnlocked", now)

    def confirm_billing(self, now):
        if not isinstance(self._state, Preparing):
            raise TransitionNotAllowed(self.state, "confirm_billing")
        self._check_booking(now)
        if self.billing_started:
            raise InvariantViolation("Тарификация уже запущена")
        self.billing_started = True
        self._record("BillingStarted", now)

    def _check_booking(self, now):
        if self.booking_deadline is None or now >= self.booking_deadline:
            raise InvariantViolation("Бесплатная бронь истекла")

    def _expire(self, now):
        if self.booking_deadline is None or now < self.booking_deadline:  # G5
            raise InvariantViolation("10 минут ещё не прошли")
        self._record("BookingExpired", now)
        return BookingExpired()

    def _check_started(self, now):
        if self.started_at is None or now < self.started_at:
            raise InvariantViolation("Время раньше начала поездки")
        if self._segment_started is not None and now < self._segment_started:
            raise InvariantViolation("Время раньше текущего тарифного интервала")

    def _close_segment(self, now):
        self._check_started(now)
        if self._segment_started is None or self._mode is None:
            raise InvariantViolation("Нет активного тарифа")
        self._segments.append((self._mode, TripPeriod(self._segment_started, now)))
        self._segment_started = self._mode = None

    def _change_rate(self, now, new_mode):
        self._close_segment(now)
        self._mode = new_mode
        self._segment_started = now

    def _finish_request(self, now, allowed, current_state):
        self._check_started(now)
        if type(allowed) is not bool:
            raise InvalidValueObject("parking_allowed должен быть true/false")
        if not allowed:  # G11: реакция без смены состояния
            self._record("InvalidParkingNotified", now)
            return current_state
        self.parking_allowed = True  # G7
        self._record("PhotoRequested", now)
        return WaitingPhoto()

    def _long_ride(self, now, current_state):
        self._check_started(now)
        if now <= self.started_at + timedelta(hours=12):
            raise InvariantViolation("12 часов ещё не превышены")
        if self.operator_notified:  # G12
            raise InvariantViolation("Оператор уже уведомлён")
        self.operator_notified = True
        self._record("OperatorNotified", now)
        return current_state

    def record_operator_contact(self, now, result):
        if not self.operator_notified:
            raise InvariantViolation("Оператор ещё не уведомлён")
        if not isinstance(result, str) or not result.strip():
            raise InvalidValueObject("Нужен результат контакта")
        if self.operator_contact is not None:
            raise InvariantViolation("Контакт уже записан")
        self.operator_contact = result
        self._record("OperatorContactRecorded", now, result)

    def calculate_cost(self):
        if self.finished_at is None:
            raise InvariantViolation("Поездка ещё не закончена")
        amount = Decimal("0")
        for mode, period in self._segments:
            mins = Decimal(str((period.ended_at - period.started_at).total_seconds())) / 60
            rate = (self.tariff.price_per_minute if mode == "normal"
                    else self.tariff.pause_price_per_minute)
            amount += mins * Decimal(str(rate))
        amount = max(amount, Decimal(str(self.tariff.minimum_price)))
        return float(amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    def to_dict(self):
        stamp = lambda dt: dt.isoformat(timespec="seconds") if dt else None
        return {
            "id": self.id, "scooter_id": self.scooter_id.value, "user_id": self.user_id,
            "state": self.state, "allowed_events": self.allowed_events,
            "booked_at": stamp(self.booked_at), "booking_deadline": stamp(self.booking_deadline),
            "started_at": stamp(self.started_at), "finished_at": stamp(self.finished_at),
            "lock_unlocked": self.lock_unlocked, "billing_started": self.billing_started,
            "parking_allowed": self.parking_allowed, "photo_url": self.photo_url,
            "preauth_request_id": self.preauth_request_id,
            "payment_request_id": self.payment_request_id,
            "cost": self.cost, "operator_notified": self.operator_notified,
            "operator_contact": self.operator_contact,
            "journal": [e.to_dict() for e in self.journal],
        }
