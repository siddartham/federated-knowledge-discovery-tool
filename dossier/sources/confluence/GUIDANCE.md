CONFLUENCE (documentation search via CQL):
Best for: documentation, runbooks, architectural decisions, onboarding guides, wikis
Queries use Confluence Query Language (CQL). Every query MUST include a text or title clause.

Text search (required in every query):
  text ~ "search terms"            full-text search across title, body, labels
  text ~ "\"exact phrase\""        exact phrase match (escape inner quotes)
  title ~ "search terms"           title-only text search
  title = "\"Exact Page Title\""   exact title match

Text search supports Lucene syntax inside the ~ operator:
  text ~ "deploy*"                 wildcard (* = multiple chars, ? = single char)
  text ~ "roam~"                   fuzzy match
  Note: wildcards cannot be the first character of a search term.

Filtering fields (combine with AND):
  space = "KEY"                    restrict to a space by its key
  space IN ("KEY1", "KEY2")        multiple spaces
  type = "page"                    content type: page, blogpost, attachment
  type IN (page, blogpost)         multiple types
  label = "architecture"           filter by label
  label IN ("draft", "review")     multiple labels

Date fields (support =, !=, >, >=, <, <=):
  lastModified > "2025-01-01"        modified after date (yyyy-MM-dd)
  created >= "2025-06-01"            created on or after date
  lastModified > startOfMonth("-3M") relative dates using functions

Date functions: startOfDay(), startOfWeek(), startOfMonth(), startOfYear()
                endOfDay(), endOfWeek(), endOfMonth(), endOfYear()
  Each accepts an optional increment: startOfDay("-4w"), endOfMonth("+1M")
  Increment format: (+/-)nn(y|M|w|d|h|m)

Keywords: AND, OR, NOT, ORDER BY
  Parentheses set precedence: (type = page AND space = DEV) OR label = "important"
  ORDER BY: space = DEV ORDER BY lastModified DESC

Examples:
  text ~ "kafka event source" AND type IN (page, blogpost)
  text ~ "deployment pipeline" AND space = "DEVOPS" AND lastModified > startOfMonth("-6M")
  title ~ "onboarding" AND label = "getting-started" AND type = page
  text ~ "\"error handling\"" AND space IN ("BACKEND", "SRE") ORDER BY lastModified DESC
