"""Flask: HTTP-запросы -> TripService. Домен про Flask ничего не знает."""
import os
from datetime import datetime

from flask import Flask, jsonify, request
from werkzeug.exceptions import HTTPException

from domain import (
    ClockMovedBack, GatewayUnresolved, InvariantViolation,
    InvalidValueObject, NotFound, Tariff, TransitionNotAllowed,
)
from service import TripService


def create_app():
    app = Flask(__name__)
    service = TripService()  # один сервис на всё приложение
    service.add_scooter("S1", 80)  # демонстрационный самокат
    app.extensions["trip_service"] = service

    def data():
        result = request.get_json(silent=True)
        if not isinstance(result, dict):
            raise InvalidValueObject("Нужно JSON-тело с объектом")
        return result

    def required(body, name, typ):
        if name not in body:
            raise InvalidValueObject(f"Нет поля {name}")
        value = body[name]
        if typ is bool and type(value) is not bool:
            raise InvalidValueObject(f"{name}: нужно true/false")
        if typ is int and type(value) is not int:
            raise InvalidValueObject(f"{name}: нужно целое число")
        if typ is float and type(value) not in (int, float):
            raise InvalidValueObject(f"{name}: нужно число")
        if typ is str and (not isinstance(value, str) or not value.strip()):
            raise InvalidValueObject(f"{name}: нужна непустая строка")
        return value

    def answer(trip):
        return jsonify(trip.to_dict())

    @app.errorhandler(TransitionNotAllowed)
    def bad_transition(error):
        return jsonify(error=error.code, message=str(error)), 409

    @app.errorhandler(InvariantViolation)
    @app.errorhandler(InvalidValueObject)
    @app.errorhandler(ClockMovedBack)
    def bad_input(error):
        return jsonify(error=error.code, message=str(error)), 422

    @app.errorhandler(NotFound)
    def not_found(error):
        return jsonify(error=error.code, message=str(error)), 404

    @app.errorhandler(GatewayUnresolved)
    def bad_gateway(error):
        return jsonify(error=error.code, message=str(error)), 500

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error="http_error", message=error.description), error.code

    @app.get("/health")
    def health():
        return jsonify(status="ok")

    @app.post("/scooters")
    def add_scooter():
        body = data()
        scooter = service.add_scooter(required(body, "id", str), required(body, "battery", int))
        return jsonify(scooter.to_dict()), 201

    @app.get("/scooters/<scooter_id>")
    def get_scooter(scooter_id):
        return jsonify(service.scooters.get(scooter_id).to_dict())

    @app.get("/accounts/<user_id>")
    def get_account(user_id):
        return jsonify(service.get_account(user_id).to_dict())

    @app.post("/accounts/<user_id>/demo-debt")
    def add_debt(user_id):
        amount = required(data(), "amount", float)
        return jsonify(service.add_demo_debt(user_id, amount).to_dict())

    @app.post("/trips")
    def register():
        body = data()
        tariff = Tariff(
            body.get("price_per_minute", 5),
            body.get("minimum_price", 50),
            body.get("pause_price_per_minute", 1),
        )
        trip = service.register_trip(
            required(body, "trip_id", str),
            required(body, "scooter_id", str),
            required(body, "user_id", str), tariff,
        )
        return answer(trip), 201

    @app.get("/trips/<trip_id>")
    def get_trip(trip_id):
        return answer(service.trips.get(trip_id))

    @app.post("/trips/<trip_id>/reserve")
    def reserve(trip_id):
        return answer(service.reserve(trip_id))

    @app.post("/trips/<trip_id>/request-start")
    def request_start(trip_id):
        return answer(service.request_start(trip_id))

    @app.post("/trips/<trip_id>/preauth-result")
    def preauth_result(trip_id):
        return answer(service.preauth_result(trip_id, required(data(), "success", bool)))

    @app.post("/trips/<trip_id>/unlock")
    def unlock(trip_id):
        return answer(service.unlock(trip_id))

    @app.post("/trips/<trip_id>/start-billing")
    def start_billing(trip_id):
        return answer(service.start_billing(trip_id))

    @app.post("/trips/<trip_id>/start-ride")
    def start_ride(trip_id):
        return answer(service.start_ride(trip_id))

    @app.post("/trips/<trip_id>/prepare")
    def prepare(trip_id):
        return answer(service.prepare(trip_id))

    @app.post("/trips/<trip_id>/pause")
    def pause(trip_id):
        return answer(service.pause(trip_id))

    @app.post("/trips/<trip_id>/resume")
    def resume(trip_id):
        return answer(service.resume(trip_id))

    @app.post("/trips/<trip_id>/request-finish")
    def request_finish(trip_id):
        allowed = required(data(), "parking_allowed", bool)
        return answer(service.request_finish(trip_id, allowed))

    @app.post("/trips/<trip_id>/receive-photo")
    def receive_photo(trip_id):
        photo_url = required(data(), "photo_url", str)
        return answer(service.receive_photo(trip_id, photo_url))

    @app.post("/trips/<trip_id>/payment-result")
    def payment_result(trip_id):
        return answer(service.payment_result(trip_id, required(data(), "success", bool)))

    @app.post("/trips/<trip_id>/operator-contact")
    def operator_contact(trip_id):
        return answer(service.operator_contact(trip_id, required(data(), "result", str)))

    @app.post("/internal/tick")
    def tick():
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise InvalidValueObject("Нужен JSON-объект")
        if "now" in body and "advance_minutes" in body:
            raise InvalidValueObject("Выбери now или advance_minutes")
        if "now" in body:
            if not isinstance(body["now"], str):
                raise InvalidValueObject("now должен быть строкой")
            try:
                target = datetime.fromisoformat(body["now"].replace("Z", "+00:00"))
            except ValueError:
                raise InvalidValueObject("Дата должна иметь формат ISO 8601") from None
            result = service.tick(at=target)
        else:
            result = service.tick(advance_minutes=body.get("advance_minutes"))
        return jsonify(result)

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
