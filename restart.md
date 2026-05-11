After restart, the backend will run normally, but large recommendation calibration will not automatically start.

  What runs automatically:

  - Signal orchestrator starts.
  - Normal scans run.
  - Weekly backtest scheduler starts.
  - Existing signal/factor caches are loaded from DB.
  - Any already-applied calibration data will be used.

  What you still need to run manually:

  curl -X POST "http://localhost:8020/api/performance/calibrate-recommendations/jobs?universe=nifty100&horizons=swing,positional&period=5y&stride=5&concurrency=3&apply=true"

  Then poll the returned job_id:

  curl "http://localhost:8020/api/performance/calibrate-recommendations/jobs/YOUR_JOB_ID"

  Paper trade CSV import also needs to be run manually if the CSV changes:

  curl -X POST "http://localhost:8020/api/performance/paper-trades/import-csv"

  So: restart is enough for normal app usage, but for the new large-scale calibration improvement, run the calibration curl once after restart.

