# Lab server <-> cloud sync (conflict-safe)

Lab server (edge, school LAN) = the authority. cPanel (cloud) = online copy.

## Nothing about scheduling was changed
* Booking layout, forms, templates, models, slot rules (`_slot_error`, capacity, opening hours,
  `cap_student_count`) and the 6-character reservation-code format are exactly as in the uploaded project.
  `scheduling/utils.py` is NOT modified. The sync layer calls the SAME functions to judge bookings.
* Only 4 existing files are touched (everything else is the new `cloud_sync/` app):
  - `scheduling/views.py`  — access checks only (`@staff_only` / `@login_required`), see the .patch
  - `notifications/services.py` — 2 decorators so reservation-code emails wait for confirmation
  - `compulab/urls.py` — one line: `path('api/', include('cloud_sync.urls'))`
  - `compulab/settings.py` — add `CLOUD_SYNC_ROLE` (below)

## The conflict rule
Only the **edge** can make a booking final. A booking made on the cloud is *provisional* until the
edge accepts it using the normal slot rules. Overlap -> the cloud booking is rejected (request declined,
reason saved, In-Charge notified). A booking the edge holds always wins over a provisional one.
Walk-in/override still share a lab up to its PC count; everything else is exclusive.

## settings.py
Edge:  CLOUD_SERVER_URL='https://yourdomain.com'; CLOUD_SYNC_API_KEY='<same secret>'; CLOUD_SYNC_ROLE='edge'
Cloud: CLOUD_SERVER_URL='';                         CLOUD_SYNC_API_KEY='<same secret>'; CLOUD_SYNC_ROLE='cloud'
Then on BOTH: `python manage.py migrate`

## Keep identical on both servers (used to match records)
Lab names, opening/closing hours, PC counts; roster name/course code/section; instructor emails.
(The edge's rules decide, so a mismatch shows up as rejected bookings, never as double bookings.)

## Reservation codes
Same 6-character format everywhere. If the same code was ever issued on both servers, the edge
gives the new booking a fresh 6-character code before confirming it; the cloud adopts it.
Codes are only emailed after confirmation, so nobody receives a code that changes.

## Roles
Admin / Lab In-Charge create and approve. Instructors only submit requests. Students never log in;
they receive notifications. approve/decline/delete previously had no login or role check; now they do.

## Notifications
Reservation-code emails on the cloud are held until the lab server confirms the booking.
Rejected -> "request declined" instead, never the code.

## Requests
Requests submitted on either server appear on the other. One request, one decision; the lab
server's decision wins.

## Tests
`python manage.py test cloud_sync`  (42 tests)


