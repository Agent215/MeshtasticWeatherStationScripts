# Bounded weather history and CSV exports

The existing latest, raw, and sampled API contracts remain available. New clients
opt into `GET /observations?stationId=...&from=...&to=...&view=chart&maxPoints=800`.
The response never needs to scan an entire large raw window to draw its chart.

## Chart contract

The server returns `mode` (`detail` or `aggregate`), `resolution` (`raw`, `hour`,
`day`, `month`, `year`), `count`, `totalMatched`, `coverage`, `updatedAt`, and
`timestampBasis: display`. `maxPoints` accepts 1–2000; clients normally ask for
200–800 depending on width. `resolutionYears` is present for grouped yearly
buckets. The selection clips to available records before choosing resolution.
There is no promise that unavailable history exists, and missing periods stay gaps.

Raw detail is returned for a selected available span up to 24 hours when it fits
the point budget. Larger views choose the finest stored summary level that fits.
Partial boundaries descend to smaller summaries, with raw reads confined to the
partial hours. Requests have explicit row, call, and elapsed-time budgets; failures
return a structured error rather than triggering an expensive raw fallback.

Aggregate items contain `bucketStart`, exclusive `bucketEnd`, original reading
`count`, `weather`, and `stats`. Regular numeric weather values use sum/count;
temperature charts show that mean with a min/max band. Gust uses maximum, rain
intervals and lightning strikes use totals, and wind direction uses a circular
mean. Cumulative counters and categorical values retain the last observation.
`summary.metrics` has sum/count/min/max/last/lastAt for the whole selected window.
Rain and lightning summary sections are null when no readings report the metric.
Clients must use these full-window summaries instead of averaging chart buckets.
All stored quantities use SI units; frontend conversion applies to means and extrema.

Four rollup levels live in a separate DynamoDB table. The source table's stream
drives a serialized worker that rebuilds each affected hour, then its day, month,
and year. Rebuilding rather than incrementing makes duplicate deliveries and late
uploads idempotent. It retries when the display-time GSI has not caught up with the
triggering observation. Historical raw observations remain intact.

Display time follows the existing clock policy: use source time unless it is
suspect, then use the home receiver's captured timestamp. Upload time is not the
measurement time. Summaries may lag ingestion briefly; `updatedAt` and coverage
describe their freshness. The stream handles immutable observation INSERTs;
manual historical corrections require an explicit rebuild/backfill.

## Full CSV exports

`POST /observations/exports` accepts JSON `{stationId, from, to}` with the existing
`x-weatherstation-key` header and returns HTTP 202 with jobId/status/rowsExported.
`GET /observations/exports/{jobId}` reports queued, running, ready, failed, or
cancelled. Ready responses include a presigned `downloadUrl` and `fileName`.
`DELETE` on the same job URL cancels queued/running work. All three require the key.
Repeated submissions for the same active station/window return the same job;
another active window for that station returns 429. The browser polls with backoff.

One background worker reads at most ten DynamoDB pages or about 25 seconds per
chunk, checkpoints its cursor and multipart state, then queues continuation.
CSV contains original readings, explicit UTC timestamp columns, and SI units.
Ready links last 15 minutes and can be renewed by polling the job. Job records
expire after 24 hours; private S3 files expire after two days. Cancellation,
expired leases, retries, and duplicate SQS messages preserve checkpoint safety.
Stalled jobs eventually report failure rather than remaining running forever.
Chart CSV is a separate immediate download of only the currently displayed data.

## Packaging and safe deployment

Use `scripts/aws/package_backend.py` with Python, boto3, and PyYAML. It places the
shared aggregate module into both read and rollup archives. Do not package the
read directory alone: it does not contain that shared module.

1. Save the deployed template and Lambda artifacts for rollback. Fetch/pull repos.
2. Build/upload the four Lambda packages and inspect a CloudFormation change set.
   No existing API or raw table may be replaced. Preserve all existing parameters,
   especially the shared secret and enabled display index.
3. Deploy with `EnableRollupProcessing=false`, `EnableAggregateHistory=false`.
   This adds the tables, stream, queues, private export bucket, and opt-in handlers.
4. Run `scripts/aws/backfill_history_aggregates.py --table WeatherObservations
   --rollup-table weather-station-dev-history-rollups --region us-east-1 --apply`.
   It performs one consistent scan, computes all levels, and never changes raw data.
5. Set `EnableRollupProcessing=true`, retaining `EnableAggregateHistory=false`.
   The stream starts at TRIM_HORIZON and reconciles arrivals during the backfill.
   Complete steps 3–5 within DynamoDB Streams' 24-hour retention.
6. Verify raw-versus-summary counts/totals, late arrivals, worker logs, and coverage.
   Enable `EnableAggregateHistory=true`, then verify legacy requests and new charts
   and exports. Only then deploy frontend clients.

To roll back the feature, restore prior frontends and set the aggregate flag false.
The legacy API continues to work. Keep raw observations and rollup data. Disabling
the rollup worker for over 24 hours requires another backfill before re-enabling.
Monitor Lambda errors/iterator age and the rollup/export dead-letter queues;
redrive/rebuild failed rollups after fixing the cause. Export jobs are temporary
and can be resubmitted. A synchronous chart intentionally cannot return all raw
readings for an unlimited range; use the queued export for that purpose.


## Deployment verification — 2026-10-17 UTC

`weather-station-dev` completed an in-place CloudFormation update in us-east-1.
Both feature flags are enabled, the stream mapping reports successful processing,
and the summary coverage advances with newly received observations. All five
Lambda code hashes match the packaged repository artifacts. The existing latest,
raw, and sampled requests were checked successfully throughout the rollout.

The backfill summarized 93,037 original observations into 3,509 records. An
independent fixed-cutoff verification at 2026-10-17T00:00:00Z compared 93,030 raw
readings with eight chart scenarios and all closed buckets, with zero mismatches.
A six-month request returned 125 daily points covering 80,548 readings in 584ms;
a ten-year request returned 149 daily points covering all 93,030 available
readings in 717ms. These are one-run measurements, not latency guarantees.

Real queued CSV exports completed and downloaded successfully: 190 readings for
the original custom window and 80,548 readings (14,216,817 bytes) for six months.
The downloaded row counts and display timestamps were checked against their
requested ranges. No historical raw records were changed by this rollout.
