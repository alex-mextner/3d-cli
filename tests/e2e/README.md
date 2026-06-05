# E2E Test Shape

The generated matrix in `test_cli_matrix.py` gives breadth: many commands, working
directories, and environment variants are checked with cheap real-CLI invocations.

The story tests in `test_cli_stories.py` give readability. They are short workflows a
human can scan to learn how the CLI is used: discover help, inspect parameters, save JSON
for another shell step, read structured errors, and run `doctor` as an environment report.
