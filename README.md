# gh-md-review

A `gh` CLI extension for reviewing Markdown and CSV files in a pull request. Renders docs in the browser, lets you add line-by-line comments, and posts them as a GitHub PR review.

## Install

```
gh extension install kirstenstubbs/gh-md-review
```

Requires Python 3. Dependencies (`markdown`, `lxml`) install automatically on first run.

## Usage

Run from inside any git repo:

```
gh md-review --view origin/<branch>
```

This opens a browser with all Markdown and CSV files on that branch rendered. Use the dropdown in the sidebar to switch branches without restarting.

To attach the review to a specific PR:

```
gh md-review --view origin/<branch> --pr 42
```

### Adding comments

Click any block to open the comment editor. When you're done, click **Post to GitHub** to submit all comments as a PR review. Comments are saved in `localStorage` so they persist if you reload.

### Review a diff instead

```
gh md-review main          # changes on this branch vs main
gh md-review main --pr 42  # same, targeting a specific PR
```

## Options

| Flag | Description |
|------|-------------|
| `--view <ref>` | Render a full branch (not a diff) |
| `--pr <number>` | PR number to post the review to |
| `--summary <text>` | Overall review summary |
| `--no-open` | Start the server without opening a browser |
| `--commit <sha>` | Review a single commit |

## Requirements

- [gh CLI](https://cli.github.com/) — authenticated with `gh auth login`
- Python 3
