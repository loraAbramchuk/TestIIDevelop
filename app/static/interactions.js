(() => {
  const panel = document.getElementById('interactions');
  if (!panel) return;
  const base = `/api/videos/${panel.dataset.videoId}`;
  const list = document.getElementById('comment-list');
  const likeCount = document.getElementById('like-count'), commentCount = document.getElementById('comment-count');
  const like = document.getElementById('like-toggle'), more = document.getElementById('comments-more');
  const refresh = document.getElementById('interactions-refresh'), empty = document.getElementById('comments-empty');
  const form = document.getElementById('comment-form'), input = document.getElementById('comment-text');
  const error = document.getElementById('interaction-error'), success = document.getElementById('interaction-success');
  let busy = false, loaded = false, liked = false, cursor = null;

  function setBusy(value) {
    busy = value;
    for (const button of panel.querySelectorAll('button')) button.disabled = value || (button !== refresh && !loaded);
  }
  function clearMessages() { error.textContent = ''; success.textContent = ''; }
  function failure(reason, fallback = 'Ошибка соединения. Повторите попытку.') {
    if (reason.status === 401 || (reason.status === 404 && reason.code !== 'comment_not_found')) {
      loaded = false;
      list.replaceChildren();
      likeCount.textContent = '—'; commentCount.textContent = '—';
      if (like) like.hidden = true;
      if (form) form.hidden = true;
      more.hidden = true; empty.hidden = true; cursor = null;
      error.textContent = 'Видео или обсуждение больше недоступно. Обновите страницу и проверьте вход в аккаунт.';
    } else error.textContent = reason instanceof TypeError ? fallback : reason.message;
  }
  async function read(path) {
    const response = await fetch(base + path, {credentials: 'same-origin', cache: 'no-store'});
    let data;
    try { data = await response.json(); } catch {}
    if (!response.ok) throw apiFailure(typeof data?.detail === 'string' ? data.detail : 'Сервис временно недоступен. Попробуйте позже.', response.status, data?.code);
    if (!data || typeof data !== 'object') throw apiFailure('Сервис временно недоступен. Попробуйте позже.', response.status);
    return data;
  }
  function showLikes(state) {
    liked = state.is_liked;
    likeCount.textContent = state.like_count;
    if (like) {
      like.hidden = false;
      like.textContent = liked ? 'Убрать лайк' : 'Нравится';
      like.setAttribute('aria-pressed', String(liked));
    }
  }
  function showComments(page, append = false) {
    if (!append) list.replaceChildren();
    const known = new Set([...list.children].map(node => node.dataset.commentId));
    for (const comment of page.items) {
      if (known.has(comment.id)) continue;
      const row = document.createElement('article');
      row.className = 'video-row'; row.dataset.commentId = comment.id;
      const meta = document.createElement('div'); meta.className = 'comment-meta';
      const author = document.createElement('a');
      author.href = `/channels/${comment.author.id}`; author.textContent = comment.author.name;
      const time = document.createElement('time');
      time.dateTime = comment.created_at; time.textContent = new Date(comment.created_at).toLocaleString('ru-RU');
      meta.append(author, time);
      const text = document.createElement('p'); text.className = 'comment-text'; text.textContent = comment.text;
      row.append(meta, text);
      if (comment.can_delete) {
        const button = document.createElement('button');
        button.type = 'button'; button.textContent = 'Удалить комментарий'; button.className = 'secondary';
        button.dataset.commentDelete = comment.id; row.append(button);
      }
      list.append(row); known.add(comment.id);
    }
    commentCount.textContent = page.total_count;
    cursor = page.next_cursor; more.hidden = !cursor;
    empty.hidden = list.children.length !== 0;
  }
  async function reload() {
    const [likes, comments] = await Promise.all([read('/likes'), read('/comments')]);
    loaded = true;
    showLikes(likes); showComments(comments);
    if (form) form.hidden = false;
  }
  refresh.addEventListener('click', async () => {
    if (busy) return;
    clearMessages(); setBusy(true);
    try { await reload(); } catch (reason) { failure(reason); }
    finally { setBusy(false); }
  });
  if (like) like.addEventListener('click', async () => {
    if (busy) return;
    clearMessages(); setBusy(true);
    try { showLikes(await apiRequest(base + '/likes', liked ? 'DELETE' : 'PUT')); }
    catch (reason) { failure(reason); }
    finally { setBusy(false); }
  });
  more.addEventListener('click', async () => {
    if (busy || !cursor) return;
    clearMessages(); setBusy(true);
    try { showComments(await read('/comments?cursor=' + encodeURIComponent(cursor)), true); }
    catch (reason) { failure(reason); }
    finally { setBusy(false); }
  });
  if (form) form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy) return;
    if (!input.value.trim()) { error.textContent = 'Введите текст комментария.'; return; }
    const text = input.value;
    clearMessages(); setBusy(true); input.disabled = true;
    let created = false;
    try {
      await apiRequest(base + '/comments', 'POST', {text});
      created = true; input.value = '';
      await reload(); success.textContent = 'Комментарий добавлен.';
    } catch (reason) {
      failure(reason, 'Не удалось подтвердить отправку. Обновите обсуждение перед повторной попыткой.');
      if (created) success.textContent = 'Комментарий добавлен. Обновите обсуждение, чтобы увидеть изменения.';
    } finally { input.disabled = false; setBusy(false); }
  });
  list.addEventListener('click', async event => {
    const button = event.target.closest('[data-comment-delete]');
    if (!button || busy || !window.confirm('Удалить ваш комментарий?')) return;
    clearMessages(); setBusy(true);
    let deleted = false;
    try {
      await apiRequest(base + '/comments/' + button.dataset.commentDelete, 'DELETE');
      deleted = true; await reload(); success.textContent = 'Комментарий удалён.';
    } catch (reason) {
      if (reason.code === 'comment_not_found') {
        try { await reload(); success.textContent = 'Комментарий уже удалён.'; }
        catch (refreshError) { failure(refreshError); }
      } else {
        failure(reason);
        if (deleted) success.textContent = 'Комментарий удалён. Обновите обсуждение, чтобы увидеть изменения.';
      }
    } finally { setBusy(false); }
  });
  setBusy(true);
  reload().catch(reason => failure(reason)).finally(() => setBusy(false));
})();
