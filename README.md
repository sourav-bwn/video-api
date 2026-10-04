# video-api

Small yt-dlp API behind the video downloader on souravgarai.vercel.app.
GET /api/info?url= returns title, thumbnail and available heights. GET /api/download?url=&q=720 returns the file.
Limits: 300 MB per file, 6 downloads per 10 minutes per IP. No files are kept after the request.
