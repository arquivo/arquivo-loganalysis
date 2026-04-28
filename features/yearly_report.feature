Feature: Yearly traffic report
  As a system administrator
  I want to view annual summaries of HTTP traffic
  So that I can understand year-over-year usage patterns

  Background:
    Given a directory of Apache log files

  Scenario: Yearly report returns totals and monthly breakdown
    Given the Flask application is running with multi-year log data
    When I request /api/yearly for year "2025" and category "all"
    Then the response status is 200
    And the response contains the year "2025"
    And the response contains total_requests greater than 0
    And the response contains a monthly breakdown

  Scenario: Yearly report rejects an invalid category
    Given the Flask application is running
    When I request /api/yearly with an invalid category
    Then the response status is 400

  Scenario: Yearly report rejects a non-numeric year
    Given the Flask application is running
    When I request /api/yearly with a non-numeric year
    Then the response status is 400

  Scenario: Month selection scopes stats to the chosen calendar month
    Given the Flask application is running with traffic in January and February 2025
    When I request /api/stats for January 2025 only
    Then the response includes January 2025 traffic
    And February 2025 traffic is absent from the response
