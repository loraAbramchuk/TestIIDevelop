(() => {
  const panel = document.getElementById('admin-users'); if (!panel) return;
  const labels = {user: 'Пользователь', moderator: 'Модератор', admin: 'Администратор'};
  const roles = Object.keys(labels), states = new Map(); let denied = false;
  const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(value);
  const validProfile = (value, id) => value && typeof value === 'object' && value.user && value.user.id === id && uuid(id) &&
    typeof value.user.email === 'string' && typeof value.user.display_name === 'string' && roles.includes(value.user.role) &&
    typeof value.user.created_at === 'string' && Number.isFinite(Date.parse(value.user.created_at)) && Number.isInteger(value.admin_count) && value.admin_count >= 1;
  function controls() {
    for (const [card, state] of states) {
      const form = card.querySelector('form'), last = card.dataset.role === 'admin' && Number(panel.dataset.adminCount) <= 1;
      for (const field of form.elements) field.disabled = denied || state.busy || state.stale;
      for (const option of form.elements.role.options) option.disabled = last && option.value !== 'admin';
      form.querySelector('button').disabled ||= last && form.elements.role.value !== 'admin';
      card.querySelector('[data-last-admin]').hidden = !last;
      card.querySelector('[data-user-refresh]').disabled = denied || state.busy;
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
    if (!states.get(card).dirtyRole) card.querySelector('form').elements.role.value = result.user.role;
    states.get(card).stale = false;
  }
  async function refreshCard(card, preserveError = false) {
    const state = states.get(card); state.busy = true; controls();
    const note = card.querySelector('[data-user-read]'), error = card.querySelector('[data-user-error]');
    if (!preserveError) { error.textContent = ''; card.querySelector('[data-user-success]').textContent = ''; }
    note.textContent = 'Получаем актуальные сведения…';
    try {
      const result = await apiRead(`/api/admin/users/${card.dataset.userId}`, data => validProfile(data, card.dataset.userId));
      render(card, result); note.textContent = 'Сведения обновлены. Несохранённый текст сохранён.';
    } catch (failure) {
      state.stale = true; note.textContent = 'Сведения не получены. Повторите обновление; текст сохранён.';
      if (!preserveError) error.textContent = failure instanceof TypeError ? 'Ошибка соединения.' : failure.message;
      if ([401,403].includes(failure.status)) loseAccess('Доступ к управлению пользователями больше недоступен. Проверьте вход и роль.');
    } finally { state.busy = false; controls(); }
  }
  for (const card of panel.querySelectorAll('.admin-user')) {
    const form = card.querySelector('form'), state = {busy: false, stale: false, dirtyRole: false}; states.set(card, state);
    form.elements.role.addEventListener('change', () => { state.dirtyRole = form.elements.role.value !== card.dataset.role; controls(); });
    card.querySelector('[data-user-refresh]').addEventListener('click', () => refreshCard(card));
    form.addEventListener('submit', async event => {
      event.preventDefault(); if (denied || state.busy || state.stale) return;
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
        if ([401,403].includes(failure.status)) loseAccess('Нет доступа к управлению пользователями. Проверьте вход и роль.');
        else if (failure instanceof TypeError || failure.code === 'invalid_response' || failure.status === 409 || failure.status === 404 || failure.status >= 500) {
          state.stale = true; await refreshCard(card, true);
        }
      } finally { state.busy = false; controls(); }
    });
  }
  controls();
})();
