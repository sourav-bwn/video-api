# video-api

Small yt-dlp API behind the video downloader on souravgarai.vercel.app.
GET /api/info?url= returns title, thumbnail and available heights. GET /api/download?url=&q=720 returns the file.
Limits: 300 MB per file, 6 downloads per 10 minutes per IP. Temporary files are removed after errors or a 60-second grace period on success.

## Local checks

Install `requirements.txt`, then run `python -m unittest -v`.
The tests use mocked yt-dlp output and Flask's test client: no remote video
requests or FFmpeg installation are needed for this regression suite.

The 300 MB cap is checked again after conversion/merging. Setup, extraction
and response-construction failures remove temporary files and release the
worker slot. Successful responses schedule temporary-file removal after
60 seconds; this grace period is not a guarantee that a slow client has
finished streaming. Worker slots cover preparation, not client transfer time.
