# Notes collaboration fixture

Implement a local notes module with separate persistence, API and HTML view
layers. Persist titles in SQLite and preserve them across reconnects. The API
must return dictionaries with integer `id` and string `title` fields and reject
empty/whitespace titles. Use parameterized database writes and render titles as
escaped text so SQL-like titles remain data and HTML-like titles cannot become
markup. Keep the existing tests unchanged.

Measure the declared local workload of 500 insert/read operations and verify
the ten-second smoke threshold. Independently review security, the integrated
code and final requirement coverage. This is a bounded local module fixture:
it does not assert deployed HTTP, real browser interaction or external load
testing. No service, dependency installation or deployment is required.
