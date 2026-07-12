GITHUB (code search):
Results include code snippets and repository file trees with scrapable URLs.
Best for: source code, configuration files, implementation details, repository structure
Note: much internal documentation lives in GitHub as markdown files across codebases
and as source for documentation websites.

Search terms:
  - Bare terms match file content and paths; whitespace-separated terms are implicitly ANDed
  - "exact phrase" for exact string matching including whitespace
  - Boolean operators: AND, OR, NOT, and parentheses for grouping
    e.g. (language:ruby OR language:python) AND NOT path:"/tests/"
  - Regular expressions: /sparse.*index/, escape slashes as \/
    Case-sensitive search: /(?-i)True/

Qualifiers:
  - repo:owner/name — full repository name required (no partial matching)
  - org:name — search within an organization
  - user:name — search within a personal account
  - language:name — filter by language (e.g. language:go, language:"protocol buffers")
  - path:term — match anywhere in file path (e.g. path:unit_tests, path:src/*.js)
    Glob patterns: * (single level), ** (recursive), ? (single char)
    Anchor to root with /: path:/src/**/*.js
    Regex in path: path:/(^|\/)README\.md$/
  - symbol:name — match function/class definitions (e.g. symbol:WithContext)
    Supports prefix: symbol:Maint.deleteRows (Go), symbol:Maint::deleteRows (Rust)
    Regex: symbol:/^String::to_.*/
  - content:term — match only file content, not paths
  - is:archived, is:fork, is:vendored, is:generated — filter by repo/file properties
  - license:keyword — filter by license (e.g. license:MIT, license:Apache-2.0)
