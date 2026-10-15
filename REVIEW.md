# REVIEW.md

I reviewed the starter `app/main.py` and grouped the defects below by their cause and fix. Function names refer to the original starter.

| # | Where | What was wrong | How to reproduce it | How I fixed it |
|---|---|---|---|---|
| 1 | Database setup, `create_employee`, `punch_in` | There were no indexes to enforce uniqueness. Checking for an existing record before inserting also allowed concurrent requests to create duplicates. | Send simultaneous requests with the same employee code or employee/date combination. | Added unique indexes and handled `DuplicateKeyError` by returning 409. Other indexes support listing and analytics. |
| 2 | MongoClient setup, `create_employee` | Database reads returned naive UTC datetimes, while employee creation used naive local time. This made timezone handling inconsistent. | Run the app in a different host timezone or compare a database timestamp with an aware IST datetime. | Enabled timezone-aware database reads, used UTC for server timestamps, and explicitly treated legacy naive datetimes as UTC. |
| 3 | `compute_late_minutes`, `punch_in` | Punch-in dates depended on the host timezone, and shift start was constructed in the punch timestamp's timezone instead of IST. | A 09:30 IST punch represented as 04:00 UTC could be compared with a 09:30 UTC shift start. | Converted timestamps to UTC and calculated attendance dates and shift boundaries in IST. |
| 4 | `compute_late_minutes` | Minutes were rounded down before checking the grace period. | With a 09:30 shift, a 09:40:01 punch incorrectly returned zero late minutes. | Checked whether elapsed seconds exceed 600 before calculating whole late minutes. |
| 5 | `compute_late_minutes`, `punch_in` | Overnight punches were assigned to the current calendar day, so lateness could also use the wrong shift start. | Punch in at 01:00 for a 22:00-06:00 shift. | Applied the overnight attendance-date rule first, then used that date to construct the shift boundaries. |
| 6 | `compute_work_hours` | Python's `round()` did not provide the required decimal half-up rounding. | A duration of 3,618 seconds is 1.005 hours and should return 1.01. | Used `Decimal` with `ROUND_HALF_UP`. |
| 7 | `compute_overtime` | Overtime was counted even when it was below 30 minutes. | Punch out at 18:50 for a shift ending at 18:30. | Returned zero unless the whole overtime minutes are at least 30. |
| 8 | `compute_overtime` | An overnight shift's end time was placed on the shift's starting date. | Punch out at 06:40 the next morning after a 22:00-06:00 shift. | Moved the shift end to the following day for overnight shifts. |
| 9 | `EmployeeIn` | Employee fields were missing validation, and equal shift start and end times were accepted. | Submit an invalid employee code, email, calendar date, 25:00 shift time, or identical shift times. | Added field constraints, calendar validation, and a check that the shift times differ. |
| 10 | `PunchInIn` | Punch-in accepted any status string. | Submit `LEAVE` or an unknown status. | Restricted punch-in to `PRESENT`, `WFH`, and `ON_DUTY`. |
| 11 | `PunchInIn` | Timestamp validation allowed coercion and did not enforce the required range. | Submit a string, float, boolean, seconds-scale timestamp, explicit null, or an out-of-range value. | Required a bounded integer in epoch milliseconds. An omitted timestamp defaults to the current time. |
| 12 | `health` | The endpoint returned success without checking MongoDB. | Stop MongoDB and call `/health`. | Added a database ping and returned 503 on failure. Readiness also requires successful index initialization. |
| 13 | Employee and attendance responses | Timestamps were returned as date strings instead of epoch milliseconds, including timestamps nested inside correction history. | Inspect `created_at`, `punch_in`, or a timestamp inside `history[].changes`. | Added recursive timestamp conversion at the API boundary. |
| 14 | List endpoints | Employee pagination skipped the first page, and neither list endpoint enforced pagination limits. | Request page 1, page 0, a negative page size, or a page size above 100. | Used `(page - 1) * page_size` and validated page and page-size bounds. |
| 15 | `list_employees` | The total counted every employee even when a department filter was applied. | Request one department and compare the total with its employee count. | Used the same filter for both the page query and the count. |
| 16 | `list_employees`, `list_attendance` | Employee listing had no required ordering, and attendance listing had no employee-code tiebreaker. | Insert employees out of order or list several attendance records with the same date. | Sorted employees by code, and attendance by date descending followed by employee code ascending. |
| 17 | `punch_in` | The code tried to use an employee record without checking whether it existed. | Punch in using an unknown employee code. | Returned 404 before calculating or inserting attendance. |
| 18 | `punch_in` | Fractional seconds were retained instead of being truncated before calculations. | Submit a timestamp with milliseconds near the grace-period boundary. | Truncated punch timestamps to whole seconds before calculation, storage, and return. |
| 19 | `punch_in`, `list_attendance` | Responses included a generated MongoDB ID that was outside the API contract. | Inspect an attendance response for `id` or `_id`. | Removed database IDs from public records and used employee code and date to identify attendance. |
| 20 | `list_attendance` | Date filters and statuses were not validated. | Submit an impossible date, a reversed date range, or an unknown status. | Validated dates, range order, and status before querying MongoDB. |
| 21 | `list_attendance` | Legacy records could omit fields required in the response. | Read a seeded record without `history` or `half_day`. | Added the documented defaults when serializing records, including an empty history and `half_day: false`. |

I also checked these following and make sure that, their behavior works correctly:

- `load_dotenv()` already gives real environment variables priority. I made `override=False` explicit.
- The customer supplies `emp_code`, which remains the public employee identifier.
- Pydantic ignores extra request fields by default, which matches the requirement to ignore client-supplied derived values.
- `PRESENT` is the correct default punch-in status. `WFH` and `ON_DUTY` also count as present.
- Synchronous PyMongo calls are suitable inside FastAPI's regular `def` routes, which run in a worker pool.
