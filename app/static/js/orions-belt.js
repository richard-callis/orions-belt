/* Orion's Belt — shared client utilities */

// Auth token helper (cookie-based auth is primary; this is a no-op stub
// kept for compatibility with first_run.html's startApp() call)
function setAuthToken(token) {
  // Token is set as an httponly cookie by the server; nothing to do client-side
}
