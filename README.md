# Read-Only Canvas CLI

A CLI for reading Canvas information.

The program uses your local Google Chrome Canvas session to authenticate, then makes read-only Canvas API requests. It can list courses, assignments, grades, announcements, people, pages, and modules, and it can mirror course file links locally.

## Requirements

- macOS
- Python 3.11+
- Google Chrome
- A Canvas account

## Install

Run the installer:

```bash
curl -fsSL https://raw.githubusercontent.com/t0masGutierrez/canvas/main/install.sh | bash
```

After installation, the `canvas` command should be available. 

## First-Time Setup

Save your Canvas institution URL:

```bash
canvas setup https://school.instructure.com
```

Log into Canvas using Google Chrome:

```bash
canvas courses
```

Wait about 30 seconds after logging in to Canvas for the program to authenticate. In the future, if your Canvas account is logged out then you will need to reauthenticate. 
