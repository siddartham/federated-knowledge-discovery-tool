GITHUB ISSUES (issue & pull-request search):
Best for: bug reports, feature discussions, PR review conversations, decision rationale
recorded in tickets, and "why did we do X" history that lives in issue/PR threads.
Note: this searches issue/PR titles, bodies, and comments — not source code. For code,
config, or file contents use the github_code source instead.

Search terms:
  - Bare keywords match issue/PR titles and bodies; whitespace-separated terms are ANDed
  - "exact phrase" for exact string matching
  - Exclude a term with a leading minus: -flaky

Qualifiers:
  - repo:owner/name — restrict to one repository (full name required)
  - org:name — search across an organization
  - is:issue / is:pr — restrict to issues or pull requests
  - is:open / is:closed / is:merged — filter by state
  - author:username — opened by a user
  - assignee:username — assigned to a user
  - label:"name" — filter by label (quote labels containing spaces)
  - in:title / in:body / in:comments — scope where keywords match
  - created:>=YYYY-MM-DD, updated:>=YYYY-MM-DD — date filters

Examples:
  session token TTL repo:acme/auth-service is:pr is:merged
  "rate limit" org:acme is:issue is:closed label:"incident"
  onboarding in:title author:alice
