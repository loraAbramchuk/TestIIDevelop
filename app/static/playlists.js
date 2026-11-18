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
  let busy = false, cursor = root.dataset.nextCursor || null, canManage = root.dataset.canManage === 'true';
  const limit = Number(root.dataset.limit);
  function rows() { return [...grid.querySelectorAll('[data-video-id]')]; }
  function controls(value) {
    busy = value;
    for (const control of root.querySelectorAll('button, input')) control.disabled = value;
    const current = rows();
    current.forEach((row, index) => {
      const actions = row.querySelector('.playlist-actions');
      if (!actions) return;
      actions.hidden = !canManage;
      row.querySelector('[data-move-up]').disabled = value || index === 0;
      row.querySelector('[data-move-down]').disabled = value || (index === current.length - 1 && !cursor);
    });
  }
  function messages() { error.textContent = ''; success.textContent = ''; }
  function failed(reason) {
    error.textContent = reason instanceof TypeError ? 'Ошибка соединения. Повторите попытку.' : reason.message;
    if (reason.status === 401) {
      canManage = false;
      if (management) management.hidden = true;
    }
    if (reason.status === 401 || reason.code === 'playlist_changed' || reason.code === 'playlist_not_found') {
      grid.replaceChildren(); cursor = null; more.hidden = true;
      document.getElementById('playlist-count').textContent = '—';
      document.getElementById('playlist-empty').hidden = true;
    }
  }
  async function read(suffix = '') {
    const response = await fetch(base + suffix, {credentials: 'same-origin', cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch {}
    if (!response.ok || !data || typeof data !== 'object') throw apiFailure(typeof data?.detail === 'string' ? data.detail : 'Сервис временно недоступен. Попробуйте позже.', response.status, data?.code);
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
      row.querySelector('.hint').textContent = `${video.duration_seconds.toFixed(1)} с${video.visibility === 'private' ? ' · Приватное видео' : ''}`;
      row.querySelector('.playlist-actions').hidden = !canManage;
      grid.append(fragment); known.add(video.id);
    }
    cursor = page.next_cursor; more.hidden = !cursor;
    document.getElementById('playlist-count').textContent = page.visible_count;
    document.getElementById('playlist-empty').hidden = rows().length !== 0;
  }
  async function reload() {
    const [profile, page] = await Promise.all([read(), read('/items?limit=' + limit)]);
    canManage = profile.can_manage;
    if (management) management.hidden = !canManage;
    document.getElementById('playlist-heading').textContent = profile.title;
    document.title = `${profile.title} — ${root.dataset.appName}`;
    render(page);
  }
  async function nextPage() { render(await read('/items?limit=' + limit + '&cursor=' + encodeURIComponent(cursor)), true); }
  async function run(action) {
    if (busy) return;
    messages(); controls(true);
    try { await action(); } catch (reason) {
      if (reason.code === 'video_not_found') {
        try { await reload(); } catch (refreshError) { failed(refreshError); }
      }
      failed(reason);
    }
    finally { controls(false); }
  }
  refresh.addEventListener('click', () => run(reload));
  more.addEventListener('click', () => run(nextPage));
  if (rename) rename.addEventListener('submit', event => {
    event.preventDefault();
    const title = rename.elements.title.value;
    run(async () => {
      const profile = await apiRequest(base, 'PATCH', {title});
      rename.elements.title.value = profile.title;
      document.getElementById('playlist-heading').textContent = profile.title;
      document.title = `${profile.title} — ${root.dataset.appName}`;
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
      await reload(); success.textContent = 'Видео добавлено.';
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
      await reload(); success.textContent = 'Плейлист обновлён.';
    });
  });
  const remove = document.getElementById('playlist-delete');
  if (remove) remove.addEventListener('click', () => {
    if (busy || !window.confirm('Удалить плейлист? Видео сохранятся.')) return;
    run(async () => { await apiRequest(base, 'DELETE'); window.location.assign('/my-playlists'); });
  });
  controls(false);
})();
