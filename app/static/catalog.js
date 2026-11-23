// Personalized channel results must re-check ACL after browser-history restoration.
window.addEventListener('pageshow', event => {
  if (event.persisted) window.location.reload();
});
