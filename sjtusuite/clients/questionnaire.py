"""
SJTU Questionnaire Client - Fetch questionnaires and submissions from wj.sjtu.edu.cn.

Uses the Smart Questionnaire Service API (wj.sjtu.edu.cn/api/v1) with a JWT
token obtained via JAccount OAuth (client_id used by the official web frontend).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator
from urllib.parse import quote

import requests
from requests.cookies import create_cookie

from sjtusuite.auth import JACLogin, OAuthClientBase

WJ_CLIENT_ID = "IK54Cb2cze7k0uaZTZ9S"
WJ_BASE_URL = "https://wj.sjtu.edu.cn"
WJ_API_BASE = f"{WJ_BASE_URL}/api/v1"
WJ_TOKEN_URL = f"{WJ_BASE_URL}/user/login"
WJ_REQUEST_TIMEOUT_SECONDS = 10.0

# The frontend defaults the data table to 20 rows per page.
WJ_DEFAULT_PAGE_SIZE = 20


class QuestionnaireAPIError(RuntimeError):
    """Raised when the questionnaire service API returns an unexpected result."""


@dataclass(slots=True, frozen=True)
class QuestionnaireSummary:
    questionnaire_id: int
    name: str
    alias: str
    description: str
    submit_count: int | None
    created_at: str | None
    updated_at: str | None
    raw: dict[str, Any]


@dataclass(slots=True, frozen=True)
class Question:
    question_id: int
    title: str
    question_type: int
    required: bool
    raw: dict[str, Any]


@dataclass(slots=True, frozen=True)
class AnswerSheet:
    answersheet_id: int
    user_name: str | None
    user_account: str | None
    user_organization: str | None
    finish_at: str | None
    duration: str | None
    ip_address: str | None
    answers: dict[str, str]
    raw: dict[str, Any]


class QuestionnaireClient(OAuthClientBase):
    """Client for the SJTU Smart Questionnaire Service (wj.sjtu.edu.cn)."""

    def __init__(self, jac_login: JACLogin, session_file: str = "questionnaire_client.cookies", name: str = "QuestionnaireClient"):
        super().__init__(name, session_file)
        self.jac_login = jac_login
        self.token: str | None = None
        self.logged_in = False

    # ------------------------------------------------------------------ auth

    def validate_session(self) -> bool:
        if not self.token:
            self.token = self._token_from_cookie_jar()
        if not self.token:
            return False
        try:
            response = self._api_request("GET", "/manage/user/profile", retry_auth=False)
            return bool(response and response.get("success"))
        except QuestionnaireAPIError:
            self.token = None
            return False

    def _token_from_cookie_jar(self) -> str | None:
        for cookie in self.session.cookies:
            if cookie.name == "sjtu_token" and cookie.value:
                return cookie.value
        return None

    def login(self, force: bool = False) -> None:
        if not force and self.validate_session():
            return
        self.token = self._fetch_token()
        self._set_token_cookie(self.token)
        self.session.headers.update({"auth": self.token})
        if not self.validate_session():
            raise QuestionnaireAPIError("Questionnaire login failed: token rejected.")
        self.logged_in = True
        self.save_session()
        self.logger.debug("Questionnaire token acquired (len=%d).", len(self.token))

    def _fetch_token(self) -> str:
        """Run the JAccount OAuth code flow and exchange the code for the wj JWT."""
        redirect_uri = (
            f"{WJ_TOKEN_URL}?channel=jaccount&retUrl="
            + quote(f"{WJ_BASE_URL}/questionnaire/list", safe="")
        )
        authorize_url = (
            "https://jaccount.sjtu.edu.cn/oauth2/authorize"
            f"?client_id={WJ_CLIENT_ID}"
            "&response_type=code&scope=profile"
            f"&redirect_uri={quote(redirect_uri, safe='')}"
        )
        final_redirect_url = self.jac_login.login(authorize_url)
        code = self._extract_oauth_code(final_redirect_url)

        # The code exchange requires the login page as Referer (the API rejects
        # bare code requests without it with "bad redirect url").
        self.session.get(final_redirect_url, allow_redirects=False, timeout=WJ_REQUEST_TIMEOUT_SECONDS)
        response = self.session.get(
            f"{WJ_API_BASE}/auth/auth",
            params={"code": code},
            headers={"Referer": final_redirect_url, "Accept": "application/json"},
            timeout=WJ_REQUEST_TIMEOUT_SECONDS,
        )
        payload = self._parse_json(response, "auth/auth")
        token = payload.get("data")
        if not token or not isinstance(token, str):
            raise QuestionnaireAPIError(f"Questionnaire token exchange failed: {payload.get('message')}")
        return token

    @staticmethod
    def _extract_oauth_code(redirect_url: str) -> str:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(redirect_url).query)
        code = query.get("code", [None])[0]
        if not code:
            raise QuestionnaireAPIError(f"No OAuth code in redirect URL: {redirect_url}")
        return code

    def _reset_auth(self) -> None:
        self.token = None
        self.logged_in = False
        self.session.headers.pop("auth", None)
        self._set_token_cookie("")

    def _set_token_cookie(self, token: str) -> None:
        self.session.cookies.set_cookie(
            create_cookie("sjtu_token", token, domain="wj.sjtu.edu.cn", path="/")
        )

    # ------------------------------------------------------------------ core

    def _api_request(self, method: str, path: str, *, params: dict[str, Any] | None = None,
                     json_data: Any = None, retry_auth: bool = True) -> Any:
        if not self.token:
            self.token = self._token_from_cookie_jar()
        if not self.token:
            self.login()
        url = path if path.startswith("http") else f"{WJ_API_BASE}{path}"
        try:
            response = self.session.request(
                method,
                url,
                params=params,
                json=json_data,
                timeout=WJ_REQUEST_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            raise QuestionnaireAPIError(f"Questionnaire request failed for {method} {url}: {exc}") from exc

        if response.status_code == 401 and retry_auth:
            self.logger.debug("Questionnaire token expired, re-authenticating...")
            self._reset_auth()
            self.login(force=True)
            return self._api_request(method, path, params=params, json_data=json_data, retry_auth=False)

        payload = self._parse_json(response, path)
        if not payload.get("success"):
            raise QuestionnaireAPIError(f"Questionnaire API error ({path}): {payload.get('message')}")
        return payload

    @staticmethod
    def _parse_json(response: requests.Response, context: str) -> dict[str, Any]:
        try:
            return response.json()
        except ValueError as exc:
            raise QuestionnaireAPIError(
                f"Questionnaire API returned non-JSON response ({context}): HTTP {response.status_code}"
            ) from exc

    # ------------------------------------------------------------ public API

    def get_profile(self) -> dict[str, Any]:
        """Current user profile (cheap session validator)."""
        return self._api_request("GET", "/manage/user/profile")["data"]

    def list_questionnaires(self, include_archived: bool = True) -> list[QuestionnaireSummary]:
        """List questionnaires created by (or shared with) the current user."""
        payload = self._api_request("GET", "/manage/questionnaire/created", params={"archive": 0})
        data = payload.get("data") or {}
        items: list[dict[str, Any]] = list(data.get("unarchived") or [])

        shared = self._api_request("GET", "/manage/questionnaire/created/shared")
        items.extend(shared.get("data") or [])

        if include_archived:
            archived = self._api_request("GET", "/manage/questionnaire/created", params={"archive": 1})
            items.extend((archived.get("data") or {}).get("archived") or [])

        return [self._to_questionnaire_summary(item) for item in items if isinstance(item, dict)]

    @staticmethod
    def _to_questionnaire_summary(item: dict[str, Any]) -> QuestionnaireSummary:
        return QuestionnaireSummary(
            questionnaire_id=item.get("id") or 0,
            name=item.get("name") or "",
            alias=item.get("alias") or "",
            description=item.get("description") or "",
            submit_count=(item.get("setting") or {}).get("submit_count"),
            created_at=item.get("created_at"),
            updated_at=item.get("updated_at"),
            raw=item,
        )

    def get_questionnaire(self, questionnaire_id: int) -> dict[str, Any]:
        """Full questionnaire definition (questions, settings, permissions)."""
        return self._api_request("GET", f"/manage/questionnaire/created/{questionnaire_id}/get")["data"]

    def list_questions(self, questionnaire_id: int) -> list[Question]:
        """Question metadata: id, title, type, required, options."""
        data = self._api_request("GET", f"/manage/questionnaire/created/{questionnaire_id}/questions")["data"]
        return [
            Question(
                question_id=q.get("id") or 0,
                title=q.get("title") or "",
                question_type=q.get("type") or 0,
                required=bool(q.get("required")),
                raw=q,
            )
            for q in data or []
        ]

    def get_overview(self, questionnaire_id: int) -> dict[str, Any]:
        """Submission statistics (submit_count, view_count, average_duration...)."""
        return self._api_request("GET", f"/manage/questionnaire/created/{questionnaire_id}/data/overview")["data"]

    def list_submission_rows(self, questionnaire_id: int, page: int = 1,
                             page_size: int = WJ_DEFAULT_PAGE_SIZE) -> tuple[list[dict[str, Any]], int]:
        """
        One page of submissions as raw table rows (question-id keyed answers plus
        submitter metadata). Returns (rows, total).
        """
        params = json.dumps({"current": page, "pageSize": page_size})
        payload = self._api_request(
            "GET",
            f"/manage/questionnaire/created/{questionnaire_id}/data/table-rows",
            params={"params": params, "sort": "{}", "filter": "{}"},
        )
        data = payload.get("data") or {}
        return list(data.get("data") or []), data.get("total") or 0

    def iter_submission_rows(self, questionnaire_id: int, page_size: int = WJ_DEFAULT_PAGE_SIZE) -> Iterator[dict[str, Any]]:
        """Yield every submission row, transparently paging through the table."""
        page = 1
        while True:
            rows, total = self.list_submission_rows(questionnaire_id, page=page, page_size=page_size)
            yield from rows
            if page * page_size >= total or not rows:
                return
            page += 1

    def list_submissions(self, questionnaire_id: int) -> list[AnswerSheet]:
        """
        All submissions as parsed AnswerSheet objects. Answer values are keyed
        by question title (question-id keyed raw values are kept in `.raw`).
        """
        questions = {q.question_id: q for q in self.list_questions(questionnaire_id)}
        sheets: list[AnswerSheet] = []
        for row in self.iter_submission_rows(questionnaire_id):
            answers: dict[str, str] = {}
            for key, value in row.items():
                qid = int(key) if str(key).isdigit() else None
                if qid is not None and qid in questions:
                    answers[questions[qid].title] = self._flatten_answer(value)
            sheets.append(
                AnswerSheet(
                    answersheet_id=row.get("id") or 0,
                    user_name=row.get("user_name"),
                    user_account=row.get("user_account"),
                    user_organization=row.get("user_organization"),
                    finish_at=row.get("finish_at"),
                    duration=row.get("duration"),
                    ip_address=row.get("ip_address"),
                    answers=answers,
                    raw=row,
                )
            )
        return sheets

    def get_submission(self, answersheet_id: int) -> AnswerSheet:
        """Full detail of one submission (including per-answer question metadata)."""
        data = self._api_request("GET", f"/manage/answersheet/{answersheet_id}/get")["data"]
        answers: dict[str, str] = {}
        for item in data.get("answers") or []:
            question = item.get("question") or {}
            if question.get("title") is not None:
                answers[question["title"]] = self._flatten_answer(item.get("answer"))
        user = data.get("user") or {}
        return AnswerSheet(
            answersheet_id=data.get("id") or 0,
            user_name=user.get("user_name"),
            user_account=row_account(user),
            user_organization=user.get("organization"),
            finish_at=data.get("finish_at"),
            duration=None,
            ip_address=data.get("ip_address"),
            answers=answers,
            raw=data,
        )

    @staticmethod
    def _flatten_answer(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return "; ".join(QuestionnaireClient._flatten_answer(v) for v in value)
        return json.dumps(value, ensure_ascii=False)


def row_account(user: dict[str, Any]) -> str | None:
    return user.get("user_account") or user.get("account")


if __name__ == "__main__":
    import logging

    from sjtusuite.auth import get_test_jac_login
    from sjtusuite.core import log

    log.DEFAULT_LOG_LEVEL = logging.INFO
    jac_login = get_test_jac_login()
    client = QuestionnaireClient(jac_login)
    client.login()
    for q in client.list_questionnaires(include_archived=False):
        print(f"[{q.questionnaire_id}] {q.name} (alias={q.alias})")
        rows, total = client.list_submission_rows(q.questionnaire_id)
        print(f"    submissions: {total}")
        for row in rows[:3]:
            print("   ", {k: v for k, v in row.items() if not str(k).isdigit()})
