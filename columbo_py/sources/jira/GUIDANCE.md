JIRA (issue tracker search via JQL):
Best for: bug reports, incidents, planned work, sprint items, release-blocking tickets
Queries use Jira Query Language (JQL). Every query MUST include a text clause
(text ~, summary ~, or description ~). If you omit ORDER BY, results are sorted by
most recently updated.

Text search (required in every query):
  text ~ "search terms"            full-text across summary, description, comments
  text ~ "\"exact phrase\""        exact phrase match (escape inner quotes)
  summary ~ "search terms"         match the ticket title only
  description ~ "search terms"     match the description body only

Filtering fields (combine with AND):
  project = "ENG"                  restrict to a project by its key
  project IN ("ENG", "SRE")        multiple projects
  status = "In Progress"           by workflow status
  statusCategory = "Done"          coarse state: To Do / In Progress / Done
  type = Bug                       issue type: Bug, Story, Task, Epic, Incident
  reporter = "alice"               opened by a user
  assignee = "bob"                 assigned to a user
  labels = "backend"               filter by label
  priority >= High                 filter by priority

Date fields (support =, !=, >, >=, <, <=):
  updated >= "2025-01-01"          updated on or after date (yyyy-MM-dd)
  created >= -30d                  relative dates: -Nd, -Nw, -Nm, -Ny
  resolved <= endOfMonth()         relative-date functions

Keywords: AND, OR, NOT, IN, ORDER BY
  Parentheses set precedence: (type = Bug AND priority >= High) OR labels = "hotfix"
  ORDER BY: project = ENG ORDER BY updated DESC

Examples:
  text ~ "session token" AND project = "AUTH" AND type = Bug
  text ~ "deploy failure" AND status != Done ORDER BY created DESC
  summary ~ "onboarding" AND labels = "getting-started"
  text ~ "\"rate limit\"" AND project IN ("API", "SRE") AND updated >= -90d
