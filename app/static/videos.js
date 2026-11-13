async function videoRequest(url, method, payload) {
  const parse = async response => {
    try { return await response.json(); }
    catch { return {detail: 'Сервис временно недоступен. Попробуйте позже.'}; }
  };
  const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'same-origin', cache: 'no-store'});
  const csrf = await parse(csrfResponse);
  if (!csrfResponse.ok || !csrf.csrf_token) throw new Error(typeof csrf.detail === 'string' ? csrf.detail : 'Обновите страницу и повторите попытку.');
  const response = await fetch(url, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const data = response.status === 204 ? null : await parse(response);
  if (!response.ok) {
    if (Array.isArray(data.detail)) throw new Error('Проверьте название, описание и ID пользователя.');
    throw new Error(data.detail || 'Не удалось выполнить запрос.');
  }
  return data;
}

for (const id of ['video-create', 'video-edit', 'video-grant']) {
  const form = document.getElementById(id);
  if (!form) continue;
  form.addEventListener('submit', async event => {
    event.preventDefault();
    const button = form.querySelector('button[type=submit]');
    const error = form.querySelector('[data-error]');
    const success = form.querySelector('[data-success]');
    error.textContent = ''; if (success) success.textContent = '';
    button.disabled = true;
    try {
      const data = Object.fromEntries(new FormData(form));
      if (id === 'video-create') {
        const video = await videoRequest('/api/videos', 'POST', data);
        window.location.assign(`/studio/videos/${video.id}`);
      } else if (id === 'video-edit') {
        await videoRequest(`/api/videos/${form.dataset.videoId}`, 'PATCH', data);
        success.textContent = 'Изменения сохранены.';
      } else {
        const userId = data.user_id.trim();
        if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(userId)) throw new Error('Введите корректный ID пользователя.');
        await videoRequest(`/api/videos/${form.dataset.videoId}/grants/${encodeURIComponent(userId)}`, 'PUT');
        window.location.reload();
      }
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
    } finally { button.disabled = false; }
  });
}

for (const button of document.querySelectorAll('[data-revoke]')) {
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await videoRequest(`/api/videos/${button.dataset.videoId}/grants/${button.dataset.revoke}`, 'DELETE');
      window.location.reload();
    } catch (failure) {
      document.getElementById('grant-error').textContent = 'Не удалось отозвать доступ. Повторите попытку.';
      button.disabled = false;
    }
  });
}
