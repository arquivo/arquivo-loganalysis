Feature: Yearly traffic report PDF download
  As a system administrator
  I want to download an annual PDF report of HTTP traffic
  So that I can share and archive year-over-year usage summaries

  Background:
    Given a directory of Apache log files

  Scenario: Download PDF for a valid year and category
    Given the Flask application is running with multi-year log data
    When I request /api/yearly/download for year "2025" and category "all"
    Then the response status is 200
    And the response Content-Type is "application/pdf"

  Scenario: Download rejects a non-numeric year
    Given the Flask application is running with multi-year log data
    When I request /api/yearly/download with a non-numeric year
    Then the response status is 400

  Scenario: Download rejects an invalid category
    Given the Flask application is running with multi-year log data
    When I request /api/yearly/download with an invalid category
    Then the response status is 400
