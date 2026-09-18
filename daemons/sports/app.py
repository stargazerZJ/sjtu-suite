"""Flask application factory for the sports reservation daemon."""

from __future__ import annotations

import json
import logging

from flask import Flask, jsonify, render_template_string, request
from sjtusuite.clients.sports import SPORTS_TIME_SLOTS
from sjtusuite.servers.base import get_client_ip

from .daemon import SportsReservationDaemon
from .dashboard import create_dashboard_html


def create_app(daemon: SportsReservationDaemon) -> Flask:
    app = Flask(__name__)
    dashboard_html = create_dashboard_html()

    @app.route("/")
    def dashboard():
        return render_template_string(
            dashboard_html, time_slots=json.dumps(SPORTS_TIME_SLOTS)
        )

    @app.route("/api/status")
    def status():
        return jsonify(daemon.service_status())

    @app.route("/api/catalog/venues")
    def catalog_venues():
        search = request.args.get("search", "")
        try:
            venues = daemon.list_venues(search)
            return jsonify({"venues": venues})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/catalog/venues/<venue_id>")
    def catalog_venue_detail(venue_id: str):
        try:
            venue = daemon.get_venue_detail(venue_id)
            return jsonify({"venue": venue})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/catalog/venues/<venue_id>/availability")
    def catalog_availability(venue_id: str):
        motion = request.args.get("motion", "")
        if not motion:
            return jsonify({"error": "motion is required"}), 400
        try:
            availability = daemon.get_availability(venue_id, motion)
            return jsonify({"availability": availability})
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs", methods=["GET", "POST"])
    def jobs():
        if request.method == "GET":
            return jsonify({"jobs": [job.to_dict() for job in daemon.list_jobs()]})
        try:
            job = daemon.create_job(request.get_json(force=True))
            return jsonify({"job": job.to_dict()}), 201
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs/<job_id>", methods=["PATCH", "DELETE"])
    def job_detail(job_id: str):
        try:
            if request.method == "DELETE":
                daemon.delete_job(job_id)
                return jsonify({"ok": True})
            job = daemon.update_job(job_id, request.get_json(force=True))
            return jsonify({"job": job.to_dict()})
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.route("/api/jobs/<job_id>/run", methods=["POST"])
    def run_job(job_id: str):
        try:
            payload = request.get_json(force=True, silent=True) or {}
            result = daemon.run_job(
                job_id,
                dry_run=bool(payload.get("dry_run", False)),
                triggered_by="manual",
            )
            return jsonify({"result": result})
        except KeyError:
            return jsonify({"error": "Job not found"}), 404
        except Exception as exc:
            return jsonify({"error": str(exc)}), 400

    @app.after_request
    def log_request(response):
        daemon._log(
            logging.DEBUG,
            "Handled HTTP request.",
            method=request.method,
            path=request.path,
            status_code=response.status_code,
            client_ip=get_client_ip(),
        )
        return response

    return app
