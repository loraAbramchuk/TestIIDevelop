async function authResponseData(response) {
  try {
    const data = await response.json();
    if (data && typeof data === 'object' && !Array.isArray(data)) return data;
  } catch {}
  throw new Error('Сервис временно недоступен. Попробуйте позже.');
}

async function authRequest(action, payload) {
  const csrfResponse = await fetch('/api/auth/csrf', { credentials: 'same-origin', cache: 'no-store' });
  const csrf = await authResponseData(csrfResponse);
  if (!csrfResponse.ok) throw new Error(typeof csrf.detail === 'string' ? csrf.detail : 'Не удалось открыть сессию. Попробуйте позже.');
  if (typeof csrf.csrf_token !== 'string' || !csrf.csrf_token) throw new Error('Обновите страницу и повторите попытку.');
  const response = await fetch(`/api/auth/${action}`, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  if (!response.ok) {
    const data = await authResponseData(response);
    if (Array.isArray(data.detail)) {
      const labels = { email: 'Email', password: 'Пароль', display_name: 'Имя' };
      const fields = [...new Set(data.detail.map(error => labels[error.loc.at(-1)] || 'Данные'))];
      throw new Error(`Проверьте поля: ${fields.join(', ')}.`);
    }
    throw new Error(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос. Попробуйте позже.');
  }
}

const form = document.querySelector('#auth-form');
if (form) form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = form.querySelector('button[type=submit]');
  const error = document.querySelector('#form-error');
  error.textContent = '';
  button.disabled = true;
  try {
    await authRequest(form.dataset.action, Object.fromEntries(new FormData(form)));
    window.location.assign(form.dataset.next || '/');
  } catch (failure) {
    error.textContent = failure instanceof TypeError ? 'Ошибка соединения. Попробуйте ещё раз.' : failure.message;
  } finally { button.disabled = false; }
});

const logout = document.querySelector('#logout');
if (logout) logout.addEventListener('click', async () => {
  logout.disabled = true;
  try {
    await authRequest('logout');
    window.location.assign('/');
  } catch (failure) {
    document.querySelector('#session-error').textContent = 'Не удалось выйти. Попробуйте ещё раз.';
    logout.disabled = false;
  }
});
