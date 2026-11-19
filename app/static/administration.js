(() => {
  const panel = document.getElementById('admin-users'); if (!panel) return;
  const labels = {user: 'Пользователь', moderator: 'Модератор', admin: 'Администратор'};
  const roles = Object.keys(labels), states = new Map(); let denied = false, accessUnknown = false, accessCheck = null;
  const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value);
  const validProfile = (value, id) => value && typeof value === 'object' && value.user && value.user.id === id && uuid(id) &&
    typeof value.user.email === 'string' && typeof value.user.display_name === 'string' && roles.includes(value.user.role) &&
    typeof value.user.created_at === 'string' && Number.isFinite(Date.parse(value.user.created_at)) && Number.isInteger(value.admin_count) && value.admin_count >= 1;
  function controls() {
    for (const [card, state] of states) {
      const form = card.querySelector('form'), last = card.dataset.role === 'admin' && Number(panel.dataset.adminCount) <= 1;
      for (const field of form.elements) field.disabled = denied || accessUnknown || state.busy || state.stale;
      for (const option of form.elements.role.options) option.disabled = last && option.value !== 'admin';
      form.querySelector('button').disabled ||= last && form.elements.role.value !== 'admin';
      card.querySelector('[data-last-admin]').hidden = !last;
      card.querySelector('[data-user-refresh]').disabled = denied || state.busy || Boolean(accessCheck);
    }
  }
  function loseAccess(message) {
    denied = true; document.getElementById('admin-access-message').textContent = message;
    for (const node of document.querySelectorAll('[data-admin-panel], [data-admin-nav]')) node.hidden = true;
    controls();
  }
  function render(card, result) {
    card.dataset.role = result.user.role; panel.dataset.adminCount = String(result.admin_count);
    document.getElementById('admin-count').textContent = result.admin_count;
    card.querySelector('[data-user-role]').textContent = labels[result.user.role];
    card.querySelector('[data-user-name]').textContent = result.user.display_name;
    card.querySelector('[data-user-email]').textContent = result.user.email;
    const state = states.get(card), select = card.querySelector('form').elements.role;
    if (!state.dirtyRole || select.value === result.user.role) {
      select.value = result.user.role; state.dirtyRole = false;
    }
    state.stale = false;
  }
  async function verifyAccess() {
    if (accessCheck) return accessCheck;
    accessUnknown = true; controls();
    accessCheck = (async () => {
      const sessionError = document.getElementById('admin-session-error');
      try {
        const actor = await apiRead('/api/auth/me', data => data && uuid(data.id) && roles.includes(data.role));
        if (actor.id !== panel.dataset.viewerId) {
          loseAccess('Аккаунт изменился. Откройте панель заново для текущего пользователя.'); return false;
        }
        if (actor.role !== 'admin') {
          loseAccess('Нет доступа к управлению пользователями: текущая роль больше не администратор.'); return false;
        }
        accessUnknown = false; sessionError.textContent = ''; return !denied;
      } catch (failure) {
        if (failure.status === 401) loseAccess('Сессия больше не действует. Войдите в аккаунт заново.');
        else sessionError.textContent = 'Не удалось проверить права. Текст сохранён; повторите обновление пользователя.';
        return false;
      } finally { accessCheck = null; controls(); }
    })();
    controls(); return accessCheck;
  }
  async function refreshCard(card, preserveError = false) {
    const state = states.get(card); state.busy = true; controls();
    const note = card.querySelector('[data-user-read]'), error = card.querySelector('[data-user-error]');
    if (!preserveError) { error.textContent = ''; card.querySelector('[data-user-success]').textContent = ''; }
    note.textContent = 'Получаем актуальные сведения…';
    try {
      if (accessUnknown && !(await verifyAccess())) {
        state.stale = true; note.textContent = 'Права не подтверждены. Текст сохранён; повторите обновление.'; return;
      }
      const read = () => apiRead(`/api/admin/users/${card.dataset.userId}`, data => validProfile(data, card.dataset.userId));
      let result;
      try { result = await read(); }
      catch (failure) {
        if (![401,403].includes(failure.status) || !(await verifyAccess())) throw failure;
        result = await read(); // One read-only retry after checking the current session.
      }
      render(card, result); note.textContent = 'Сведения обновлены. Несохранённый текст сохранён.';
    } catch (failure) {
      state.stale = true; note.textContent = 'Сведения не получены. Повторите обновление; текст сохранён.';
      if (!preserveError) error.textContent = failure instanceof TypeError ? 'Ошибка соединения.' : failure.message;
      if ([401,403].includes(failure.status) && !denied) accessUnknown = true;
    } finally { state.busy = false; controls(); }
  }
  for (const card of panel.querySelectorAll('.admin-user')) {
    const form = card.querySelector('form'), state = {busy: false, stale: false, dirtyRole: false}; states.set(card, state);
    form.elements.role.addEventListener('change', () => { state.dirtyRole = form.elements.role.value !== card.dataset.role; controls(); });
    card.querySelector('[data-user-refresh]').addEventListener('click', () => refreshCard(card));
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (denied || accessUnknown || state.busy || state.stale) return;
      const payload = Object.fromEntries(new FormData(form)); payload.expected_role = card.dataset.role;
      if (card.dataset.userId === panel.dataset.viewerId && payload.role !== 'admin' && !window.confirm('Ваша роль изменится, и доступ к панели администратора закроется. Продолжить?')) return;
      const error = card.querySelector('[data-user-error]'), success = card.querySelector('[data-user-success]');
      error.textContent = ''; success.textContent = ''; card.querySelector('[data-user-read]').textContent = ''; state.busy = true; controls();
      try {
        const result = await apiRequest(`/api/admin/users/${card.dataset.userId}/role`, 'PATCH', payload, data => validProfile(data, card.dataset.userId) && typeof data.changed === 'boolean' && roles.includes(data.actor_role) && data.user.role === payload.role);
        render(card, result);
        if (result.changed) { form.elements.reason.value = ''; state.dirtyRole = false; form.elements.role.value = result.user.role; success.textContent = 'Роль изменена. Действие записано в журнал.'; }
        else success.textContent = 'Эта роль уже назначена. Новая запись в журнал не добавлена; текст сохранён.';
        if (result.actor_role !== 'admin') loseAccess('Ваша роль изменена. Панель администратора теперь недоступна; текущая сессия сохранена.');
      } catch (failure) {
        error.textContent = failure instanceof TypeError ? 'Результат изменения не подтверждён. Проверьте сведения перед повтором.' : failure.message;
        if ([401,403].includes(failure.status)) {
          state.stale = true;
          if (await verifyAccess()) {
            if (failure.code === 'csrf_invalid') error.textContent = 'Токен безопасности отклонён. Права подтверждены; повторите назначение вручную.';
            await refreshCard(card, true);
          } else card.querySelector('[data-user-read]').textContent = 'Права не подтверждены. Несохранённый текст сохранён.';
        }
        else if (failure instanceof TypeError || ['invalid_response', 'request_timeout'].includes(failure.code) || failure.status === 409 || failure.status === 404 || failure.status >= 500) {
          state.stale = true; await refreshCard(card, true);
        }
      } finally { state.busy = false; controls(); }
    });
  }
  controls();
})();
