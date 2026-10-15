# Decisions

1. **Indexes.** I ended up adding unique indexes on emp_code and on the combination of emp_code and date to prevent duplicate employees and attendance records. The other indexes support various department filters, joining date checks, attendance sorting, and finding the latest punch in. I skipped a separate status index because the compound status/date/employee index already handles that filter and the required ordering.

2. **Punch-in race.** If two punch-in requests arrive at the same time, both may find the employee and try to insert a record. MongoDB’s unique index on employee code and date allows only one insert. The other raises DuplicateKeyError, which the API returns as a 409 conflict. This works without an application lock or a separate duplicate check before insertion.

3. **Ties.** Employees are ranked by their total late minutes, and the limit is applied to those ranks. If employees tie at the cutoff, all of them are included and listed by employee code. Tied employees share a rank, so the ranking follows 1, 2, 2, 4.

4. **Headcount.** The department summary starts with employees who joined by the end of the month, then looks up their attendance records. Employees with no records still count toward headcount and contribute zero attendance totals. Average work hours are calculated by dividing total hours by the number of qualifying records, rather than averaging each employee’s average.

5. **At 100x.** At 100 times the data, I would first check the query plans to find the slowest operations. I would then consider storing daily department summaries and updating them whenever attendance is recorded or corrected. This would reduce the work needed to calculate department trends on each request
