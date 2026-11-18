(() => {
  const form = document.getElementById('video-report');
  if (!form) return;
  let busy = false, sent = false;
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || sent) return;
    const payload = Object.fromEntries(new FormData(form));
    const error = form.querySelector('[data-error]'), success = form.querySelector('[data-success]');
    error.textContent = ''; success.textContent = ''; busy = true;
    for (const control of form.elements) control.disabled = true;
    try {
      await apiRequest(`/api/videos/${form.dataset.videoId}/reports`, 'POST', payload, data => ModerationData.receipt(data, form.dataset.videoId) && data.status === 'open');
      sent = true; success.textContent = 'Жалоба отправлена на рассмотрение.';
    } catch (failure) {
      error.textContent = failure instanceof TypeError || failure.code === 'invalid_response' ? 'Не удалось подтвердить отправку. Можно повторить: открытая жалоба не дублируется.' : failure.message;
    } finally {
      busy = false;
      for (const control of form.elements) control.disabled = sent;
    }
  });
})();
