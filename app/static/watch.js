const player = document.getElementById('video-player');
if (player) {
  player.addEventListener('error', () => {
    document.getElementById('player-error').textContent = 'Не удалось загрузить видео. Обновите страницу и проверьте соединение или доступ к ролику.';
  }, true);
  // Reauthorize a page restored from browser history; previously received bytes
  // cannot be revoked, but a restored page must not bypass a new ACL check.
  window.addEventListener('pageshow', event => { if (event.persisted) window.location.reload(); });
}
