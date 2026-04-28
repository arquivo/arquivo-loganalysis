Feature: Available years discovery
  As a user
  I want the year selector to show only years that have data
  So that I cannot select a year with no traffic

  Background:
    Given a directory of Apache log files

  Scenario: API returns years that have data
    Given the Flask application is running with multi-year log data
    When I request /api/years with category "all"
    Then the response status is 200
    And the response contains a list of years
    And the year "2025" is in the available years

  Scenario: API returns empty list when no data exists
    Given the Flask application is running with an empty database
    When I request /api/years with category "all"
    Then the response status is 200
    And the available years list is empty

  Scenario: API rejects an invalid category for years
    Given the Flask application is running with an empty database
    When I request /api/years with category "invalid"
    Then the response status is 400
