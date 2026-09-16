# SJTU Questionnaire Service (wj.sjtu.edu.cn) Research

Last updated: 2026-09-16
Site: `https://wj.sjtu.edu.cn` (智能问卷服务平台, Network Information Center)

## Authentication

The frontend (umi/ant-design-pro SPA) authenticates with a JWT stored as cookie
`sjtu_token` (also sent as header `auth` by the frontend request layer).

OAuth flow (jAccount):

1. Authorize URL:
   `https://jaccount.sjtu.edu.cn/oauth2/authorize?client_id=IK54Cb2cze7k0uaZTZ9S&response_type=code&scope=profile&redirect_uri=<login page URL>`
   The redirect_uri must be the login page `https://wj.sjtu.edu.cn/user/login`
   with query params preserved (`channel=jaccount`, optional `retUrl`).
2. After JAccount login the browser lands on
   `https://wj.sjtu.edu.cn/user/login?channel=jaccount&code=<code>`.
3. The SPA exchanges the code via `GET /api/v1/auth/auth?code=<code>`.
   - **The request must carry a `Referer` header pointing at the login page
     (the exact URL containing the code), otherwise the server responds
     `400 {"message":"bad redirect url"}`.**
   - Response: `{"success":true,"data":"<JWT>"}`.
4. The JWT is valid ~24h (exp claim). Use as cookie `sjtu_token` or header `auth`.
   Invalid/expired token -> `401 {"message":"需要登录"}`.

The JAccount session cookies cached in `jac_login.cookies` make re-login work
without touching credentials.

## API base

All JSON endpoints live under `https://wj.sjtu.edu.cn/api/v1`.
Response envelope: `{"success": bool, "message": str, "data": ..., "code": int}`.

## Confirmed endpoints

### User

- `GET /api/v1/manage/user/profile` - current user (good session validator)

### Questionnaire list

- `GET /api/v1/manage/questionnaire/created?archive=0` - unarchived own forms
  (`data.unarchived[]`)
- `GET /api/v1/manage/questionnaire/created?archive=1` - archived own forms
  (`data.archived[]`)
- `GET /api/v1/manage/questionnaire/created/shared` - forms shared to me
  (`data[]` list)
- `GET /api/v1/manage/questionnaire-archive` - archive folders

Each questionnaire item: `id`, `name`, `alias` (public path token), `description`,
`setting` (permissions, quota, notify email, ...), timestamps.

### Questionnaire detail

- `GET /api/v1/manage/questionnaire/created/<id>/get` - full definition
- `GET /api/v1/manage/questionnaire/created/<id>/questions` - question metadata
  (`id`, `type`, `title`, `required`, `option`, ...)

### Results / statistics

- `GET /api/v1/manage/questionnaire/created/<id>/data/overview` -
  `submit_count`, `today_submit_count`, `view_count`, `average_duration`
- `GET /api/v1/manage/questionnaire/created/<id>/data/questions` -
  per-question submit counts
- `GET /api/v1/manage/questionnaire/created/<id>/data/table-columns` -
  ant-design table column defs (maps question ids to titles, includes
  submitter metadata columns)
- `GET /api/v1/manage/questionnaire/created/<id>/data/table-rows?params=<json>&sort=<json>&filter=<json>` -
  paginated submissions

### Pagination format

`table-rows` takes URL-encoded JSON blobs:

```txt
params={"current":1,"pageSize":20}&sort={}&filter={}
```

- `current` is 1-based page number, `pageSize` rows per page (default 20).
- Response: `data.data[]` rows, `data.total` total count.

Row shape: submitter metadata keys (`id` = answersheet id, `user_name`,
`user_account`, `user_organization`, `duration`, `finish_at`, `ip_address`,
`status`, `tags`, `avatar`, ...) plus one key per question id with the answer
value.

Search filters (from the frontend `nsearch`) are merged into `params`, e.g.
`finish_at_start` / `finish_at_end` (format `YYYY-MM-DD HH:mm:ss.SSS`), and
question-id keyed values for per-question filtering.

### Single submission

- `GET /api/v1/manage/answersheet/<sheet_id>/get` - full submission:
  `answers[]` (each with embedded `question` and `answer`), `user` (name,
  email, mobile, organization), `begin_at`/`finish_at`, `browser`, `status`,
  reject/revoke flags.

### Export (browser-download URLs, need auth cookie)

- `GET /api/v1/manage/questionnaire/created/<id>/data/table-export/excel?params=<json>`
- `GET /api/v1/manage/answersheet/<sheet_id>/export/excel`
- `GET /api/v1/manage/questionnaire/created/<id>/answersheet/<sheet_id>/archive`
  (attachments)

### Tags

- `POST /api/v1/manage/answersheet/tag` (data)
- `DELETE /api/v1/manage/answersheet/tag/<id>/delete`

## Public answering side (not covered by the client)

- Fill page: `https://wj.sjtu.edu.cn/q/<alias>`
- Template/share endpoints under `/manage/questionnaire/created/<id>/template`,
  `/clone`, `/make-private`.

## Implementation notes

- The frontend uses umi-request with prefix `/api/v1`, `credentials: same-origin`,
  and injects header `auth: <sjtu_token>` when the cookie exists.
- Codes are single-use; the exchange must happen promptly after authorize.
- `Referer` enforcement on `/auth/auth` is the only anti-replay quirk found.
- Question types seen: 1 = fill-in, 3 = single choice (answer is option value
  like `"option2"`), 10/11 = multiple choice / dropdown.
