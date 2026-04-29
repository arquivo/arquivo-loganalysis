Feature: Apache log parsing
  As a system administrator
  I want to parse Apache access log files and understand traffic patterns
  So that I can investigate issues and find top locations

  Background:
    Given a directory of Apache log files

  Scenario: Parse a standard Combined Log Format entry
    Given a log line in Combined Log Format
    When I parse the log line
    Then I get the IP address
    And I get the HTTP method
    And I get the request path
    And I get the HTTP status code
    And I get the bytes transferred

  Scenario: Parse an extended log entry with response time
    Given a log line with a response time field
    When I parse the log line
    Then I get the response time in microseconds

  Scenario: Aggregate top 10 locations
    Given a sample log file with multiple request paths
    When I parse the log directory
    Then the top paths list has at most 10 entries
    And the top paths are sorted by request count descending

  Scenario: Bot and human traffic are split
    Given a sample log file with both bot and human requests
    When I parse the log directory
    Then the human bucket counts only human requests
    And the bot bucket counts only bot requests
    And the all bucket counts everything

  Scenario: Drill-down returns paths for a top IP
    Given a sample log file with a high-volume IP
    When I parse the log directory
    Then drill-down data for that IP is available
    And the paths are ordered by request count

  Scenario: Drill-down returns IPs for a top path
    Given a sample log file with a high-volume path
    When I parse the log directory
    Then drill-down data for that path is available

  Scenario: Entries outside the date window are excluded
    Given a sample log file spanning several months
    When I parse the log directory with a 30-day window
    Then only recent entries are counted

  Scenario: Files with old dates in their name are still ingested
    Given a log file named with an old date
    When I parse the log directory with a 30-day window
    Then the file is listed as ingested

  Scenario: Gracefully skip malformed lines
    Given a log file containing some malformed lines
    When I parse the log directory
    Then the error count is greater than zero
    And valid lines are still counted

  Scenario: Cache is reused on repeated parse
    Given a sample log file that has already been parsed
    When I parse the log directory a second time
    Then the results are identical to the first parse

  Scenario: Web API returns stats when parsing is complete
    Given the Flask application is running
    When I request /api/stats for the all category
    Then the response status is 200
    And the response contains total_requests

  Scenario: Web API exposes recent log entries
    Given the Flask application is running
    When I request /api/logs
    Then the response contains log entries

  Scenario: Web API returns drill-down data for an IP
    Given the Flask application is running with traffic from a known IP
    When I request the drill-down for that IP
    Then the response contains path counts for that IP

  Scenario: Default worker count scales to CPU count
    Given PARSE_WORKERS is unset
    And a sample log file with multiple request paths
    When I parse the log directory
    Then the default worker count equals the CPU count

  Scenario: PARSE_WORKERS env var overrides the default
    Given PARSE_WORKERS is set to "3"
    And a sample log file with multiple request paths
    When I parse the log directory
    Then the workers used is 3

  Scenario: Worker processes run at a lowered OS priority so the laptop stays responsive
    Given PARSE_NICE is set to "7"
    And a sample log file with multiple request paths
    When I parse the log directory
    Then worker processes ran with a nice increment of 7

  Scenario: Malformed request lines are not counted as HTTP methods
    Given a log file containing malformed request lines
    When I parse the log directory
    Then the methods data contains only valid HTTP methods

  Scenario: Reparse cooldown prevents thrashing on active log files
    Given a log file that has just been ingested
    When the file's mtime changes but the cooldown has not expired
    Then the file is treated as current and skipped

  Scenario: Stats API accepts an explicit date range
    Given the Flask application is running
    When I request /api/stats with an explicit date range
    Then only data within that date range is returned

  Scenario: Stats API rejects an inverted date range
    Given the Flask application is running
    When I request /api/stats with from_date after to_date
    Then the response status is 400
    And the response contains a date range error

  Scenario: Re-ingesting a changed file replaces all previous data with no stale rows
    Given a log file with 3 known requests is parsed once
    When the file is modified to contain 5 requests and parsed again
    Then the total_requests in daily_totals for that source equals 5
    And the parsed_files ledger reflects the new mtime and size

  Scenario: Ingest pipeline emits progress logs for each processing phase
    Given a sample log file with multiple request paths
    And log capture is attached to the ingestor, parser, and db loggers
    When I parse the log directory
    Then the logs contain a message indicating parse started for the file
    And the logs contain a message about GeoIP enrichment with an IP count
    And the logs contain a message about DB write with row counts per table
    And the logs contain a completion message with per-phase timing breakdown
    And the logs contain a retention enforcement result message

  Scenario: Subnet drill-down aggregates IPs into /24 subnets
    Given the Flask application is running with traffic from multiple subnets
    When I request /api/drill/subnets
    Then the response contains subnet entries
    And subnets are sorted by request count descending
    And each subnet entry has ip_prefix, count, and pct fields

  Scenario: Subnet drill-down flags subnets exceeding the alert threshold
    Given the Flask application is running with a dominant subnet
    When I request /api/drill/subnets
    Then the dominant subnet is flagged as an alert

  Scenario: Subnet drill-down returns empty list when no data exists
    Given the Flask application is running
    When I request /api/drill/subnets for a future date range
    Then the response contains an empty subnets list

  # ── tar.gz archive ingestion ──────────────────────────────────────────────────

  Scenario: A tar.gz archive containing a single log file is ingested
    Given a tar.gz archive containing a single log file with 5 requests
    When I parse the log directory
    Then the total request count is 5

  Scenario: A tar.gz archive containing multiple log files is ingested and totals are summed
    Given a tar.gz archive containing 2 log files with 3 requests each
    When I parse the log directory
    Then the total request count is 6

  Scenario: A plain log file and a tar.gz archive coexist and both are ingested
    Given a plain log file with 4 requests and a tar.gz archive with 3 requests in the same directory
    When I parse the log directory
    Then the total request count is 7

  Scenario: A plain gzip-compressed log file is ingested
    Given a gzip-compressed log file with 5 requests
    When I parse the log directory
    Then the total request count is 5

  Scenario: Progress percentage never exceeds 100 when ingesting compressed and uncompressed files
    Given a plain log file with 4 requests and a tar.gz archive with 3 requests in the same directory
    When I parse the log directory with progress tracking
    Then the maximum reported progress does not exceed 100 percent

  Scenario: Startup skips re-parsing when the ledger already covers every file
    Given a sample log file that has already been parsed
    When start_parsing is invoked while the ledger is up to date
    Then the parse status is "done" without ever entering the "parsing" state
    And the status message indicates no parsing was needed
