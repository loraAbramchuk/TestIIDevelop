(() => {
  const create = document.getElementById('playlist-create');
  if (create) create.addEventListener('submit', async event => {
    event.preventDefault();
    const values = Object.fromEntries(new FormData(create)), controls = [...create.elements];
    create.querySelector('[data-error]').textContent = '';
    for (const control of controls) control.disabled = true;
    try {
      const playlist = await apiRequest('/api/playlists', 'POST', values);
      window.location.assign('/playlists/' + playlist.id);
    } catch (failure) {
      create.querySelector('[data-error]').textContent = failure instanceof TypeError ? 'Не удалось подтвердить создание. Обновите страницу перед повторной попыткой.' : failure.message;
    } finally { for (const control of controls) control.disabled = false; }
  });
  const root = document.getElementById('playlist');
  if (!root) return;
  const base = '/api/playlists/' + root.dataset.playlistId;
  const grid = root.querySelector('.video-grid'), more = document.getElementById('playlist-more');
  const error = document.getElementById('playlist-error'), success = document.getElementById('playlist-success');
  const refresh = document.getElementById('playlist-refresh'), management = document.getElementById('playlist-management');
  const rename = document.getElementById('playlist-rename'), add = document.getElementById('playlist-add');
  let busy = false, missing = false, cursor = root.dataset.nextCursor || null, canManage = root.dataset.canManage === 'true';
  let savedTitle = rename?.elements.title.value;
  const limit = Number(root.dataset.limit);
  function rows() { return [...grid.querySelectorAll('[data-video-id]')]; }
  function controls(value) {
    busy = value;
    for (const control of root.querySelectorAll('button, input')) {
      control.disabled = value || missing || (!canManage && !!management?.contains(control));
    }
    const current = rows();
    current.forEach((row, index) => {
      const actions = row.querySelector('.playlist-actions');
      if (!actions) return;
      actions.hidden = !canManage || missing;
      row.querySelector('[data-move-up]').disabled = value || missing || !canManage || index === 0;
      row.querySelector('[data-move-down]').disabled = value || missing || !canManage || (index === current.length - 1 && !cursor);
    });
  }
  function messages() { error.textContent = ''; success.textContent = ''; }
  function invalidateItems() {
    grid.replaceChildren(); cursor = null; more.hidden = true;
    document.getElementById('playlist-count').textContent = '—';
    document.getElementById('playlist-empty').hidden = true;
  }
  function showProfile(profile, acceptTitle = false) {
    if (rename) {
      // Synchronize a clean form, but keep a real draft across refreshes.
      if (acceptTitle || rename.elements.title.value === savedTitle) rename.elements.title.value = profile.title;
      savedTitle = profile.title;
    }
    canManage = profile.can_manage;
    if (management) management.hidden = !canManage;
    document.getElementById('playlist-heading').textContent = profile.title;
    document.title = `${profile.title} — ${root.dataset.appName}`;
  }
  function failed(reason) {
    error.textContent = reason instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : reason.message;
    if (reason.status === 401) {
      canManage = false;
      if (management) management.hidden = true;
    }
    if (reason.code === 'playlist_not_found' && reason.fromPlaylistRead) {
      missing = true; canManage = false;
      if (management) management.hidden = true;
      document.getElementById('playlist-gone-navigation').hidden = false;
      error.textContent = 'Плейлист удалён или больше недоступен.';
      success.textContent = '';
    }
    if (reason.status === 401 || reason.code === 'playlist_changed' || missing) {
      invalidateItems();
    }
  }
  async function read(suffix = '') {
    const response = await fetch(base + suffix, {credentials: 'same-origin', cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch {}
    if (!response.ok || !data || typeof data !== 'object') {
      const failure = apiFailure(typeof data?.detail === 'string' ? data.detail : 'Сервис временно недоступен. Попробуйте позже.', response.status, data?.code);
      failure.fromPlaylistRead = true;
      throw failure;
    }
    return data;
  }
  function render(page, append = false) {
    if (!append) grid.replaceChildren();
    const known = new Set(rows().map(row => row.dataset.videoId));
    for (const video of page.items) {
      if (known.has(video.id)) continue;
      const fragment = document.getElementById('playlist-card-template').content.cloneNode(true);
      const row = fragment.querySelector('article'); row.dataset.videoId = video.id;
      row.querySelector('.card-preview').href = '/watch/' + video.id;
      const image = row.querySelector('img'); image.src = video.thumbnail_url; image.alt = 'Превью: ' + video.title;
      const title = row.querySelector('.card-title'); title.href = '/watch/' + video.id; title.textContent = video.title;
      const author = row.querySelector('.card-author'); author.href = '/channels/' + video.author.id; author.textContent = video.author.name;
      row.querySelector('.card-avatar').textContent = [...video.author.name][0].toUpperCase();
      const seconds = Math.max(1, Math.ceil(video.duration_seconds)), minutes = Math.floor(seconds / 60);
      row.querySelector('.card-duration').textContent = (minutes >= 60 ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}` : String(minutes)) + ':' + String(seconds % 60).padStart(2, '0');
      row.querySelector('.hint').textContent = new Date(video.created_at).toLocaleDateString('ru-RU') + (video.visibility === 'private' ? ' · Приватное видео' : '');
      row.querySelector('.playlist-actions').hidden = !canManage;
      grid.append(fragment); known.add(video.id);
    }
    cursor = page.next_cursor; more.hidden = !cursor;
    document.getElementById('playlist-count').textContent = page.visible_count;
    document.getElementById('playlist-empty').hidden = rows().length !== 0;
  }
  async function reload() {
    const [profile, page] = await Promise.all([read(), read('/items?limit=' + limit)]);
    showProfile(profile);
    render(page);
  }
  async function nextPage() { render(await read('/items?limit=' + limit + '&cursor=' + encodeURIComponent(cursor)), true); }
  async function handleFailure(reason) {
    if (reason.code === 'video_not_found' || reason.code === 'playlist_item_not_found' || (reason.code === 'playlist_not_found' && !reason.fromPlaylistRead)) {
      try { await reload(); }
      catch (refreshError) { invalidateItems(); failed(refreshError); return; }
    }
    failed(reason);
  }
  async function afterMutation(message) {
    invalidateItems();
    success.textContent = message + ' Обновляем список…';
    try { await reload(); success.textContent = message; }
    catch (reason) {
      invalidateItems();
      await handleFailure(reason);
      if (!missing) success.textContent = message + ' Список пока не обновлён. Нажмите «Обновить плейлист».';
    }
  }
  async function run(action) {
    if (busy || missing) return;
    messages(); controls(true);
    try { await action(); } catch (reason) { await handleFailure(reason); }
    finally { controls(false); }
  }
  refresh.addEventListener('click', () => run(reload));
  more.addEventListener('click', () => run(nextPage));
  if (rename) rename.addEventListener('submit', event => {
    event.preventDefault();
    const title = rename.elements.title.value;
    run(async () => {
      const profile = await apiRequest(base, 'PATCH', {title});
      showProfile(profile, true);
      success.textContent = 'Название сохранено.';
    });
  });
  if (add) add.addEventListener('submit', event => {
    event.preventDefault();
    let identifier;
    try {
      const url = new URL(add.elements.url.value.trim(), location.origin);
      const match = url.pathname.match(/^\/watch\/([0-9a-fA-F-]{36})\/?$/);
      if (url.origin !== location.origin || !match) throw new Error();
      identifier = match[1];
    } catch { error.textContent = 'Вставьте ссылку на страницу видео на этом сайте.'; return; }
    run(async () => {
      await apiRequest(base + '/items/' + identifier, 'PUT');
      add.elements.url.value = '';
      await afterMutation('Видео добавлено.');
    });
  });
  grid.addEventListener('click', event => {
    const button = event.target.closest('button'), row = button?.closest('[data-video-id]');
    if (!row || !canManage || busy) return;
    run(async () => {
      const video = row.dataset.videoId;
      if (button.hasAttribute('data-remove-video')) {
        await apiRequest(base + '/items/' + video, 'DELETE');
      } else {
        let current = rows().map(item => item.dataset.videoId), index = current.indexOf(video);
        const up = button.hasAttribute('data-move-up');
        while (!up && index + 2 >= current.length && cursor) {
          await nextPage(); current = rows().map(item => item.dataset.videoId); index = current.indexOf(video);
        }
        const before = up ? current[index - 1] : current[index + 2] || null;
        if (up && !before) return;
        await apiRequest(base + '/items/' + video + '/position', 'PUT', {before_video_id: before});
      }
      await afterMutation('Плейлист обновлён.');
    });
  });
  const remove = document.getElementById('playlist-delete');
  if (remove) remove.addEventListener('click', () => {
    if (busy || !window.confirm('Удалить плейлист? Видео сохранятся.')) return;
    run(async () => { await apiRequest(base, 'DELETE'); window.location.assign('/my-playlists'); });
  });
  const cleanup = document.getElementById('playlist-cleanup');
  if (cleanup) cleanup.addEventListener('click', () => {
    if (busy || missing || !window.confirm('Убрать из плейлиста все видео, которые сейчас вам недоступны? Сами видео сохранятся.')) return;
    run(async () => {
      await apiRequest(base + '/unavailable-items', 'DELETE');
      await afterMutation('Недоступные видео убраны из плейлиста.');
    });
  });
  controls(false);
})();
