# guitar-tab-sync-service

This Docker Compose service turns a tab into the following outputs:

- A Guitar Pro file (`.gp`) with synced YouTube audio.
- A [Music Pack](https://cgarst.github.io/MusicPackSpec/) (`.mpak`) with the full band and the backing track as two recordings, each with its own sync points.
- A Music Pack with the audio split into stems by [StemDeck](https://github.com/stemdeckapp/stemdeck).

It runs [teaqu/guitar-pro-youtube-sync](https://github.com/teaqu/guitar-pro-youtube-sync) and offers it as a small REST API and web UI.

## Configuration

Create a `.env` file next to `docker-compose.yml` (it is git-ignored). Only the essentials are active:

```sh
# --- Essentials ---

# Access token required on every request. Generate one with: openssl rand -hex 32
# Leave empty only if nobody else can reach the port.
AUTH_TOKEN=

# The address clients use to reach this service (include a proxy path prefix if you use one).
PUBLIC_BASE_URL=http://localhost:8080

# --- Optional ---

# Host port to publish.
# GPSYNC_PORT=8080

# Pinned versions. Set either one to latest to follow new releases.
# GPYTSYNC_VERSION=latest   (used only when GPYTSYNC_BRANCH is empty)
# STEMDECK_VERSION=0.21.0

# Use another upstream repo, and/or track one of its branches instead of a release (overrides GPYTSYNC_VERSION;
# each start installs the branch's newest commit).
# GPYTSYNC_REPO=cgarst/guitar-pro-youtube-sync
# GPYTSYNC_BRANCH=raw-flag

# Set to 0 to skip stems: the bundled StemDeck container is not started.
# STEMDECK_ENABLED=1

# Delete a finished job's files / its record and log this many minutes after it finished (0 = keep forever).
# FILE_TTL_MINUTES=0
# JOB_TTL_MINUTES=0

# The "Open in" button on a finished job. {url} is the URL-encoded file address, {url_raw} is not.
# Both are empty by default, which means no button.
# OPEN_LINK_TEMPLATE=majestictab://open?url={url}
# OPEN_LINK_LABEL=MajesticTab
```

`STEMDECK_URL` (default `http://stemdeck:8000`, the bundled StemDeck) points the app at another StemDeck. The job time
limit (`MPAK_JOB_TIMEOUT`, default 3600 seconds) is set in `docker-compose.yml`.

## Access token

Set `AUTH_TOKEN` to require a token on every request.

- API: add `token=<token>` to any request, as a query string or form field. A request without it gets `401`.
- Web pages: open `http://host:8080/?token=<token>` once. The page sets an `HttpOnly` cookie.
- Files: the file links of a finished job end in `&sig=...`. The signature covers
  that job's files, so you can share a link without exposing the token. It stops working when the token changes.

## Start container

To start the service and open the web page:

```sh
docker compose up -d --build     # the first start downloads the release (about 30 s)
open http://localhost:8080
```

## REST API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/search?q=<text>` | Search the tab source. Returns `[{song, artist, title, url}]`, best match first. `size` (1-50, default 10) sets the number of results. |
| `POST` | `/api/jobs` | Start a job. |
| `GET` | `/api/get?q=<text>` | Start a job with a plain GET. Same parameters and response as `POST /api/jobs`. |
| `GET` | `/api/jobs` | List recent jobs. |
| `GET` | `/api/job?id=<id>` | Get a job's status. Add `log=1` for the full log. |
| `DELETE` | `/api/job?id=<id>` | Delete a finished or failed job and its files (`409` while it runs). |
| `GET` | `/api/job/file?id=<id>` | Download a finished job's main file. Add `name=<file>` for another file. |
| `GET` | `/openapi.json` | OpenAPI 3.1 description of this API (needs no token). |
| `GET` | `/healthz` | Check that the service is alive. Reports the upstream release and whether stems are on. |

```sh
curl -X POST -d song=23063 localhost:8080/api/jobs
curl 'localhost:8080/api/get?q=Johann+Sebastian+Bach+Bourre+In+E+Minor&first=1'    # the same, with a GET
curl 'localhost:8080/api/search?q=Johann+Sebastian+Bach+Bourre+In+E+Minor'
curl -X POST --data-urlencode 'song=Johann Sebastian Bach Bourre In E Minor' localhost:8080/api/jobs          # 409 + matches
curl localhost:8080/api/job?id=23063-ab12cd
curl -OJ localhost:8080/api/job/file?id=23063-ab12cd
```

### Start a job

`POST /api/jobs` and `GET /api/get` take these parameters, as a form or in the query string:

- `song`: a song ID, a song page URL, or search text. You can send it as `q` instead.
- `force`: send `force=1` to run the job again when the same song is already queued, running, or done. A
  finished job is also run again, without `force`, when an older version of the service or of upstream made it.
- `first`: send `first=1` to take the first search result without asking. The job then reports `matched`: the query
  and the artist and title it resolved to.

Status codes:

- `202`: the job started.
- `200`: the same song is already queued, running, or done. The response is the existing job.
- `409`: you sent search text without `first`. The response lists `matches` (`song`, `artist`, `title`, `url`).
  Send the chosen `song=<id>` to confirm.

`GET /api/get` suits clients that can only issue a GET, such as a "From URL" import. Repeating the request is safe.

### Job status

`status` is `queued`, `running`, `done`, or `failed`. `source_url` links to the song's page at the tab source. Every job reports `duration_seconds` and `queued_seconds`.

A `running` job has `progress`: `{step, steps, name, stage, percent}`. `percent` is `null` when the stage has none.

A `done` job includes:

- `file_url` and `open_url`, which point to the main file.
- `files`, a list of `{name, file_url}` for every file the job made.
- `outputs`, the three outputs in good, better, best order. An output that could not be made has `available: false` and a
  `reason`.

Downloading returns `409` while the job isn't done, and `410` after its files were purged (see [Storage](#storage)).

### API description

`/openapi.json` describes every route, parameter and response, and declares the token as `tokenQuery`. Every response
carries `Link: </openapi.json>; rel="service-desc"` and, for search clients, a `Link` to `/opensearch.xml`
([OpenSearch](https://github.com/dewitt/opensearch)). Both follow the `PUBLIC_BASE_URL` path prefix, so the proxy
must strip the prefix before it forwards a request. API responses also allow cross-origin reads (`Access-Control-Allow-Origin: *`).

## What a job makes

One job makes every output that applies. The first of `files` is the main file.

| Tier | File | When |
|---|---|---|
| Best | `<name> (stems).mpak` | StemDeck is available |
| Better | `<name> (full band + backing).mpak` | the song has a backing video |
| Good | `<name>_synced.gp` | always |

A missing StemDeck, a missing backing video or a failed separation skips that output with a warning in the log. The job
fails only when nothing could be made or the YouTube audio could not be downloaded. Downloads are tried up to three
times, because YouTube sometimes answers with HTTP 403.

- Guitar Pro file: the tab with the full band audio trimmed to the first bar and embedded.
- Full band + backing pack: the source's full band video and backing video as two recordings, `mix` (role
  `original`) and `backing` (role `backing`). Each has its own sync file, because the bar times differ.
- Stems pack: the plain tab, the full YouTube audio, six stems (vocals, drums, bass, guitar, `keys`, other), and the
  source's bar times as the sync file. The stems come from StemDeck running as a second
  compose service. The pack has one recording, `mix`, and the stems share its sync.

To skip stems, set `STEMDECK_ENABLED=0`. On a CPU, separation takes minutes per song, and the pack is about seven times
the size of the audio. The first run also downloads the model.

## Web

`GET /get?q=<song id, URL or search text>` starts a job and opens its page; search text shows results to pick from.
The page at `/` has one box, the recent jobs, and an Edit button for deleting jobs. The page at `/job?id=<id>` shows
live status and, when done, a download button for each file and the Open in link.

## Using it with MajesticTab

In [MajesticTab](https://github.com/cgarst/majestictab), go to Settings, then Search providers, and add this service. `<address>` is the address of this service container, for example `http://localhost:8080`.

To fill in missing tabs by itself (API):

1. Add a provider with the address `<address>/api/get?q=$s`.
2. In Tools, open Fill in missing tabs and select Search.
3. MajesticTab asks the service for each song and adds the tab it gets back.

If one song matches several tabs, MajesticTab asks you to pick. Add `&first=1` to the address to take the first match. If you set `AUTH_TOKEN`, add `&token=$token` to the
address.

To check each tab yourself (web UI):

1. Add a provider with the address `<address>/get?q=$s`.
2. In Tools, open Fill in missing tabs. Select the arrow beside Search and choose this provider.
3. MajesticTab opens the service in your browser for one song. Wait for the job to finish.
4. Select Open in MajesticTab. The tab is added to that song, and MajesticTab moves to the next song.

Step 4 needs the "Open in" button (set `OPEN_LINK_TEMPLATE`; it is empty by default). If you set `AUTH_TOKEN`, add `&token=$token` to the
address.

## Storage

Jobs, their logs, and their files live in the `gpsync-data` volume. The service cleans up at start and every 10 minutes.
Both settings are off by default (`0` keeps data forever). The clock starts when a job finishes.

| Setting | Effect |
|---|---|
| `FILE_TTL_MINUTES` | Deletes a finished job's `.gp` and `.mpak` files. The job then reports `purged`, and its file URLs return `410`. Asking for the song again runs a new job. |
| `JOB_TTL_MINUTES` | Deletes the job's record and log. |

## Tests

Run a test with `python3 tests/test_<name>.py`. Most run offline. `test_mpak.py` runs against a fake StemDeck and
validates the pack with the reference tool from the public [MusicPackSpec](https://github.com/cgarst/MusicPackSpec)
repository. It clones that repository once into the system temp directory, so it needs git, Node 22+, npm and network,
and skips the validation without them. `tests/smoke.sh [base-url] [song]` exercises the API.
