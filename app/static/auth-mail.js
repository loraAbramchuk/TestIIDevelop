(() => {
  const resend = document.getElementById('verification-resend');
  const receipt = data => typeof data?.detail === 'string' && data.detail.length > 0;
  if (resend) resend.addEventListener('click', async () => {
    if (resend.disabled) return;
    resend.disabled = true;
    const status = document.getElementById('verification-status'), error = document.getElementById('verification-error');
    status.textContent = 'Отправляем письмо…'; error.textContent = '';
    try { status.textContent = (await apiRequest('/api/auth/email/request', 'POST', undefined, receipt)).detail; }
    catch (failure) { status.textContent = ''; error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Проверьте почту перед повторной отправкой.' : failure.message; }
    finally { resend.disabled = false; }
  });
  const form = document.getElementById('auth-mail-form');
  if (!form) return;
  const action = form.dataset.action, submit = form.querySelector('button[type=submit]');
  const error = document.getElementById('mail-error'), success = document.getElementById('mail-success'), progress = document.getElementById('mail-progress');
  // URL fragments never go to the HTTP server. Remove the token from browser history too.
  let token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
  if (location.hash || location.search) history.replaceState(null, '', location.pathname);
  if (action !== 'forgot-password' && !token) { error.textContent = 'Откройте ссылку из письма. Если она устарела, запросите новую.'; submit.disabled = true; }
  let busy = false, done = false;
  form.addEventListener('submit', async event => {
    event.preventDefault(); if (busy || done) return;
    const values = Object.fromEntries(new FormData(form));
    if (action === 'reset-password' && values.password !== values.confirmation) { error.textContent = 'Пароли не совпадают.'; return; }
    busy = true; error.textContent = ''; success.textContent = ''; progress.hidden = false;
    for (const control of form.elements) control.disabled = true;
    try {
      const path = action === 'forgot-password' ? 'password/request' : action === 'reset-password' ? 'password/reset' : 'email/verify';
      const payload = action === 'forgot-password' ? {email: values.email} : action === 'reset-password' ? {token, password: values.password} : {token};
      const result = await apiRequest('/api/auth/' + path, 'POST', payload, receipt);
      done = true; token = ''; success.textContent = result.detail;
      for (const input of form.querySelectorAll('input[type=password]')) input.value = '';
      if (action === 'verify-email') {
        apiRead('/api/auth/me', data => typeof data?.email_verified === 'boolean')
          .then(current => { if (current.email_verified) document.getElementById('verification-resend')?.closest('section').setAttribute('hidden', ''); })
          .catch(() => {});
      }
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Результат не подтверждён. Проверьте почту или попробуйте войти перед повтором.' : failure.message;
      if (action !== 'forgot-password' && (failure instanceof TypeError || ['request_timeout', 'invalid_response'].includes(failure.code) || failure.status >= 500)) {
        error.textContent += action === 'reset-password' ? ' Попробуйте войти с новым паролем; при необходимости запросите новую ссылку.' : ' Обновите страницу аккаунта, чтобы проверить email; при необходимости запросите новое письмо.';
      }
    } finally {
      busy = false; progress.hidden = true;
      for (const control of form.elements) control.disabled = done;
      if (document.activeElement === document.body) {
        if (done) { success.tabIndex = -1; success.focus(); }
        else submit.focus();
      }
    }
  });
})();
