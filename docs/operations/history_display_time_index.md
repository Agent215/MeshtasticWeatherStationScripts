# Display-time weather history

History ranges now use `display_ts_utc`: source time for a healthy station clock,
receiver time when the source clock is suspect. The original `OBS#<source time>`
primary keys and all observation data remain unchanged.

`ObservationDisplayTimeIndex` uses `pk` as its partition key and `history_sort`
as its sort key. Each observation has
`DISPLAY#<UTC with six fractional digits>#<original sk>`. The original key suffix
keeps distinct observations with identical display timestamps independently
pageable. `LATEST` has no `history_sort` and is excluded from this sparse index.
The index projects all attributes to preserve the raw API response. Sampled
results and rain/lightning summaries use the same indexed range. The existing
50,000-point sampled-query limit is unchanged.

## Deployment order

1. Save deployed Lambda packages and the CloudFormation template for rollback.
2. Add the index to the existing table without replacing the table.
3. Deploy the ingest writer, read handler, and index Query permission with
   `HistoryIndexName` empty. New observations acquire an index key immediately;
   reads continue using the old path during migration.
4. Install boto3 for the operator script. Run
   `python scripts/aws/backfill_history_display_index.py --table WeatherObservations --apply`.
   This adds only missing `history_sort` attributes, uses conditional updates,
   and can safely be rerun. It never replaces or deletes weather records.
5. Rerun without `--apply`; require `missing=0` and `mismatched=0`.
6. Wait for `ObservationDisplayTimeIndex` to be `ACTIVE`, and verify its item
   count and representative query results. Global secondary index reads are
   eventually consistent, so allow propagation before comparing counts.
7. Set the CloudFormation `HistoryIndexName` parameter to
   `ObservationDisplayTimeIndex`. Verify latest, one-hour, custom, sampled,
   raw ascending/descending pagination, and summary responses through the API.

The Lambda environment variable `HISTORY_INDEX_NAME` is controlled by that
parameter. Empty selects the original source-time query for staged deployment
or rollback. Setting the parameter back to empty rolls reads back without
removing the index or any historical data. Do not deploy the pre-index table
template as a routine rollback because that would remove the index.

Pagination tokens are specific to their index. An in-flight pre-migration token
must be discarded and the query restarted after cutover; the API responds with
`400 invalid_nextToken` instead of a server error for such tokens.

The station clock still needs correction upstream. Its incorrect source times
are retained for diagnostics; this change makes history selection consistent
with the timestamps already shown on the dashboard.

## Deployment verification — 2026-10-16

Deployed to `weather-station-dev` in `us-east-1` through three reviewed
CloudFormation change sets: index creation, backend code/IAM update, then read
cutover. The final stack status was `UPDATE_COMPLETE` and
`HISTORY_INDEX_NAME=ObservationDisplayTimeIndex`.

- All 32 ingest, history, and migration tests passed.
- The migration added 93,022 missing keys. A subsequent full-table audit found
  93,026 observations, zero missing keys, and zero mismatches; four new records
  already had keys from the updated ingest writer.
- A full base-table/index comparison found exactly 93,026 observations in each,
  with no missing, mismatched, or non-observation rows.
- The original custom range, `2026-10-16T13:39:00Z` through
  `2026-10-16T20:55:00Z`, returned 190 points instead of zero. Ascending and
  descending raw pagination returned the same 190 unique records across six
  pages each. Sampling to 10 points preserved the full-window summaries.
- Live windows ending `2026-10-16T23:49:00Z` returned 22 points for one hour,
  646 for one day, 4,596 for seven days, and 19,238 for thirty days. Every returned
  point's display time was checked against its requested bounds.
- The latest endpoint remained healthy. The upstream source clock was still
  flagged suspect, so the receive-time fallback remains necessary.

Public API verification was paced to respect the existing route throttles.
No frontend deployment was needed.
