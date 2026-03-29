# SJTU Sports Reservation Research

Last updated: 2026-03-29
Site: `https://sports.sjtu.edu.cn/pc`

## High-level flow

### Personal reservation (`state=1`)

1. Home page or `/Venue/1` lists venues.
2. Click a venue card to enter:
   - `#/apointmentDetails/1/:venueId/:isType/:bizId`
3. On the detail page:
   - choose sport tab (`motionType`)
   - choose date
   - choose one or more grid cells (field x hour)
   - click `立即下单`
   - site fetches reservation clause
   - user checks the terms checkbox
   - site submits encrypted order to `POST /venue/personal/ConfirmOrder`
4. Frontend stores `sessionStorage.newOrderDetailsId`
5. Frontend routes to:
   - `#/orderDetails/0`
6. Payment is launched from order details through:
   - `POST /venue/personal/orderImmediatelyPC`

### Special-event reservation (`state=0`)

This is a separate approval flow, not the normal seat-grid booking flow.

1. Route:
   - `#/apointmentDetails/0/:venueId/:isType/:bizId`
2. Fill form fields such as:
   - activity name
   - person in charge
   - college approver
   - phone
   - participant count
   - date range
   - weekly recurrence
   - time range
   - field count
3. Save or submit via:
   - `POST /venue/TbNewBizVenue/addAndUpdate`
4. Submit path then validates with:
   - `POST /venue/BizVenueInstances/siteVerification?bizVenueId=...&release=false`

## Confirmed endpoints

### Session and user state

- `GET /system/user/currentUser`
- `POST /appointment/disabled/getAppintmentAndSysUserbyUser`
- `POST /rule/getBreakRule`

### Home and venue list

- `POST /manage/motionType/typeList`
- `POST /manage/motionType/typeListByCampus`
- `POST /manage/campus/list`
- `POST /manage/venue/list`
- `POST /manage/venue/listOrderCount`
- `POST /venue/notice/newSysNotice?typeId=1`

### Venue detail page

- `POST /manage/venue/queryVenueById`
- `POST /venue/notice/newStadiumNotice?id=:venueId`
- `POST /manage/fieldDetail/queryFieldReserveSituationIsFull`
- `POST /manage/fieldDetail/queryFieldSituation`
- `POST /venue/moreSetting/getOne`
- `POST /captcha/get`
- `POST /captcha/check`

### Personal order creation and payment

- `POST /venue/personal/checkRule`
- `POST /venue/personal/ConfirmOrder`
- `GET /venue/personal/queryOrder?orderId=:orderId`
- `POST /venue/personal/orderImmediatelyPC`
- `POST /venue/personal/order/paid`
- `POST /venue/personal/removeAccompanying`
- `POST /venue/personal/confirmUpdate`

### Special-event reservation

- `POST /venue/TbNewBizVenue/getPositionUser`
- `POST /venue/TbNewBizVenue/getBizVenueDetail?bizId=:bizId`
- `POST /venue/TbNewBizVenue/addAndUpdate`
- `POST /venue/BizVenueInstances/siteVerification?bizVenueId=...&release=false`
- `POST /manage/venueType/list?venueId=:venueId`
- `POST /process/venue/specialEcho`

## Request shapes

### Venue list

`POST /manage/venue/list`

Content type:
- `application/x-www-form-urlencoded`

Observed body pattern:

```txt
pageSize=12&pageNum=1&campusIds=&typeIds=&venueName=&flag=0
```

Notes:
- `flag=0` is used on the venue list page for personal booking.
- `campusIds` and `typeIds` are sent as empty strings when unfiltered.

### Venue detail lookup

`POST /manage/venue/queryVenueById`

Body:

```txt
id=<venueId>
```

Response includes:
- `venueId`
- `venueName`
- `campusName`
- `openTime`
- `venueMobile`
- `motionTypes[]`

Each `motionTypes[]` item includes:
- `id`
- `name`
- `tension`
- `writeOffType`

### Date availability strip

`POST /manage/fieldDetail/queryFieldReserveSituationIsFull`

Observed body:

```json
{
  "id": "<venueId>",
  "feildType": "<motionTypeId>",
  "date": "2026-03-29"
}
```

Response gives the visible date tabs and a `dateId` for each day.

### Grid data for one date

`POST /manage/fieldDetail/queryFieldSituation`

Observed body:

```json
{
  "fieldType": "<motionTypeId>",
  "date": "2026-03-29",
  "venueId": "<venueId>",
  "dateId": "<dateId>"
}
```

Response shape:
- one entry per field/court
- each entry includes:
  - `fieldName`
  - `fieldId`
  - `fieldDetailStatus`
  - `priceList[]`

Each `priceList[]` item includes at least:
- `count`
- `price`
- `status`
- `sign`

Observed status meanings from frontend behavior:
- `0`: selectable
- `1`: selected in UI
- `-2`, `-1`, `-3`: not selectable

### Terms/clause before submit

`POST /venue/moreSetting/getOne`

Used when user clicks `立即下单`.

Response includes:
- `title`
- `clause`

Some venue IDs are hard-coded in the frontend to override the returned clause text.

### Personal order submit

`POST /venue/personal/ConfirmOrder`

Headers used by frontend:
- `Content-Type: application/json;charset=utf-8`
- `sid: <RSA(randomKey)>`
- `tim: <RSA(timestamp)>`
- `tid: <captchaVerification>` only after captcha challenge path

Request body:
- AES-encrypted JSON payload

Frontend payload shape before encryption:

```json
{
  "venTypeId": "<motionTypeId>",
  "venueId": "<venueId>",
  "fieldType": "<motionTypeName>",
  "returnUrl": "https://sports.sjtu.edu.cn/#/paymentResult/1",
  "scheduleDate": "2026-03-29",
  "week": "0",
  "spaces": [
    {
      "venuePrice": "3",
      "count": 1,
      "sign": "<server-provided-sign>",
      "status": 1,
      "scheduleTime": "19:00-20:00",
      "subSitename": "场地10",
      "subSiteId": "0077116b-41f2-4b36-96a1-0dc379eb3604",
      "tensity": "1",
      "venueNum": 1
    }
  ],
  "tenSity": "紧张",
  "dateId": "<dateId>"
}
```

Frontend result handling:
- `code == 0`
  - store returned order id in `sessionStorage.newOrderDetailsId`
  - route to `orderDetails`
- `code == 1002`
  - show captcha challenge
- `code == 10001`
  - force user to update profile/phone info

### Order details and payment

`GET /venue/personal/queryOrder?orderId=:orderId`

Used by `/orderDetails/:type` to render:
- selected spaces
- prices
- accompanyings
- amount due

`POST /venue/personal/orderImmediatelyPC`

## Implementation notes

- `GET /system/user/currentUser` is not a safe unauthenticated validator by itself. Without a real logged-in browser context, it can still return `{"msg":"操作成功","code":0}` while omitting the `data` object.
- When reusing the managed Playwright browser session, protected sports APIs are most reliable when called from the browser context itself instead of replaying the browser cookie through plain `requests`.
- A real end-to-end test on 2026-03-29 successfully created one unpaid personal order and initialized payment with:
  - `POST /venue/personal/ConfirmOrder`
  - `GET /venue/personal/queryOrder?orderId=:orderId`
  - `POST /venue/personal/orderImmediatelyPC`

Body:

```txt
orderId=<newOrderDetailsId>
```

If successful, frontend redirects browser to:

```txt
https://cwc2.jdcw.sjtu.edu.cn/payment/pay/pay.action?data=...&sign=...&subsysid=...&sysid=...
```

## Concrete sample captured live

Authenticated sample used on 2026-03-29:

- Venue:
  - `南区体育馆`
  - `venueId = 3f009fce-10b4-4df6-94b7-9d46aef77bb9`
- Motion type:
  - `乒乓球`
  - `motionTypeId = 28d3bea9-541d-4efb-ae46-e739a5f78d72`
- Selected slot:
  - `场地10`
  - `19:00-20:00`
  - `subSiteId = 0077116b-41f2-4b36-96a1-0dc379eb3604`
  - price `3.00`

## Useful frontend routes

- `#/Venue/1`
- `#/apointmentDetails/1/:id/:isType/:bizId`
- `#/Order`
- `#/orderDetails/:type`
- `#/paymentResult/:id`
- `#/individualOrderDetails`

## Implementation notes

- The seat-grid flow is fully client-driven until `ConfirmOrder`.
- `dateId` looks like a required server-generated token and should be treated as opaque.
- Each selectable slot carries a server-issued `sign`; that also looks required and should be passed through unchanged.
- The site enforces booking pressure rules via `tension` / `tenSity` and via `POST /venue/personal/checkRule`.
- Captcha is not always required. The frontend only adds header `tid` after the site responds with `code == 1002`.
