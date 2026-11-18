const videoRequest = apiRequest;

for (const id of ['video-create', 'video-edit']) {
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
      } else {
        await videoRequest(`/api/videos/${form.dataset.videoId}`, 'PATCH', data);
        success.textContent = 'Изменения сохранены.';
      }
    } catch (failure) {
      error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
    } finally { button.disabled = false; }
  });
}

const grantList = document.getElementById('video-grants');
if (grantList) {
  const form = document.getElementById('video-grant');
  const recipient = form.querySelector('[name=user_id]');
  const error = document.getElementById('grant-error');
  const success = document.getElementById('grant-success');
  const refresh = document.getElementById('grants-refresh');
  const next = document.getElementById('grants-next');
  const base = `/api/videos/${grantList.dataset.videoId}/grants`;
  let busy = false;
  function setBusy(value) {
    busy = value;
    for (const control of [...form.querySelectorAll('input, button'), ...grantList.querySelectorAll('button'), refresh]) control.disabled = value;
  }
  async function refreshGrants() {
    const page = new URL(window.location.href);
    const url = new URL(base, window.location.origin);
    for (const name of ['limit', 'cursor']) {
      if (page.searchParams.has(name)) url.searchParams.set(name, page.searchParams.get(name));
    }
    const response = await fetch(url, {credentials: 'same-origin', cache: 'no-store'});
    if (!response.ok) throw new Error('Не удалось обновить список разрешений. Проверьте соединение и доступ к видео.');
    const data = await response.json();
    const rows = document.createDocumentFragment();
    for (const grant of data.items) {
      const row = document.createElement('li');
      const code = document.createElement('code');
      code.textContent = grant.user_id;
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = 'Отозвать доступ';
      button.dataset.revoke = grant.user_id;
      button.dataset.videoId = grantList.dataset.videoId;
      button.disabled = busy;
      row.append(code, button); rows.append(row);
    }
    if (!data.items.length) {
      const empty = document.createElement('li');
      empty.textContent = page.searchParams.has('cursor') ? 'На этой странице разрешений нет.' : 'Дополнительных разрешений нет.';
      rows.append(empty);
    }
    grantList.replaceChildren(rows);
    next.hidden = !data.next_cursor;
    if (data.next_cursor) {
      page.searchParams.set('cursor', data.next_cursor);
      next.href = page.pathname + page.search;
    } else next.removeAttribute('href');
  }
  async function changeGrant(action, message) {
    if (busy) return;
    setBusy(true);
    error.textContent = ''; success.textContent = '';
    form.querySelector('[data-error]').textContent = '';
    let changed = false;
    try {
      await action();
      changed = true; success.textContent = message;
      await refreshGrants();
    } catch (failure) {
      error.textContent = changed ? 'Разрешение изменено, но список не обновился. Нажмите «Обновить список разрешений».' : failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
    } finally { setBusy(false); }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    const userId = recipient.value.trim();
    if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(userId)) {
      form.querySelector('[data-error]').textContent = 'Введите корректный ID пользователя.';
      return;
    }
    await changeGrant(async () => {
      await videoRequest(`${base}/${encodeURIComponent(userId)}`, 'PUT');
      recipient.value = '';
    }, 'Доступ разрешён.');
  });
  // Delegation also handles buttons added by a refreshed/paginated list.
  grantList.addEventListener('click', async event => {
    const button = event.target.closest('[data-revoke]');
    if (!button || busy) return;
    await changeGrant(() => videoRequest(`${base}/${button.dataset.revoke}`, 'DELETE'), 'Доступ отозван.');
  });
  refresh.addEventListener('click', async () => {
    if (busy) return;
    setBusy(true); error.textContent = '';
    try { await refreshGrants(); }
    catch { error.textContent = 'Не удалось обновить список разрешений. Повторите попытку.'; }
    finally { setBusy(false); }
  });
}

const deleteForm = document.getElementById('video-delete');
if (deleteForm) {
  deleteForm.addEventListener('submit', async event => {
    event.preventDefault();
    if (!window.confirm('Удалить видео? Просмотр будет закрыт сразу. Восстановить файлы после удаления нельзя.')) return;
    const button = deleteForm.querySelector('[type=submit]');
    button.disabled = true;
    try {
      await videoRequest(`/api/videos/${deleteForm.dataset.videoId}`, 'DELETE');
      window.location.assign('/studio');
    } catch (failure) {
      deleteForm.querySelector('[data-error]').textContent = failure instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : failure.message;
      button.disabled = false;
    }
  });
}
