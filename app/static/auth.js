(() => {
  const FRIENDLY = 'Сервис временно недоступен. Попробуйте позже.';
  const TIMEOUT = 'Превышено время ожидания ответа. Проверьте соединение и повторите проверку.';
  const problem = (message, properties = {}) => Object.assign(new Error(message), properties);
  const message = failure => failure instanceof TypeError ? 'Ошибка соединения. Попробуйте ещё раз.' : failure.message;
  const validUser = user => user && typeof user.id === 'string' && typeof user.email === 'string' && typeof user.display_name === 'string' && ['user', 'moderator', 'admin'].includes(user.role);

  // One deadline covers headers, JSON bodies and session confirmation. Never replay a POST.
  async function bounded(action) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try { return await action(controller.signal); }
    catch (failure) {
      if (controller.signal.aborted) throw problem(TIMEOUT, {kind: 'timeout'});
      throw failure;
    } finally { clearTimeout(timer); }
  }
  async function objectData(response) {
    try {
      const data = await response.json();
      if (data && typeof data === 'object' && !Array.isArray(data)) return data;
    } catch (failure) { if (failure.name === 'AbortError') throw failure; }
    throw problem(FRIENDLY, {kind: 'invalid_response'});
  }
  function responseFailure(response, data) {
    let detail = typeof data.detail === 'string' ? data.detail : FRIENDLY;
    if (Array.isArray(data.detail)) {
      const labels = {email: 'Email', password: 'Пароль', display_name: 'Имя'};
      const fields = [...new Set(data.detail.map(error => labels[error?.loc?.at?.(-1)] || 'Данные'))];
      detail = `Проверьте поля: ${fields.join(', ')}.`;
    }
    return problem(detail, {definitive: response.status >= 400 && response.status < 500 && response.status !== 408});
  }
  async function fetchAuth(path, options, signal) {
    return fetch('/api/auth/' + path, {...options, credentials: 'same-origin', cache: 'no-store', signal});
  }
  async function session(signal) {
    const response = await fetchAuth('me', {}, signal);
    const data = await objectData(response);
    if (response.status === 401 && typeof data.detail === 'string') return null;
    if (!response.ok) throw responseFailure(response, data);
    if (response.status !== 200 || !validUser(data)) throw problem(FRIENDLY, {kind: 'invalid_response'});
    return data;
  }
  async function mutate(action, payload, signal, started) {
    const csrfResponse = await fetchAuth('csrf', {}, signal);
    const csrf = await objectData(csrfResponse);
    if (!csrfResponse.ok) throw responseFailure(csrfResponse, csrf);
    if (typeof csrf.csrf_token !== 'string' || !csrf.csrf_token) throw problem('Обновите страницу и повторите попытку.');
    started();
    const response = await fetchAuth(action, {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
      body: payload ? JSON.stringify(payload) : undefined,
    }, signal);
    if (action === 'logout' && response.status === 204) return null;
    const data = await objectData(response);
    if (!response.ok) throw responseFailure(response, data);
    const expectedStatus = action === 'register' ? 201 : 200;
    if (action === 'logout' || response.status !== expectedStatus || !validUser(data.user) || typeof data.csrf_token !== 'string' || !data.csrf_token) {
      throw problem('Результат не подтверждён. Проверьте состояние аккаунта перед повтором.', {kind: 'invalid_response'});
    }
    return data.user;
  }
  function emailKey(email) {
    const value = email.trim().normalize('NFC').toLowerCase(), at = value.lastIndexOf('@');
    try { return value.slice(0, at) + '@' + new URL('https://' + value.slice(at + 1)).hostname; }
    catch { return value; }
  }

  const form = document.getElementById('auth-form');
  if (form) {
    const submit = form.querySelector('button[type=submit]'), check = document.getElementById('auth-check');
    const error = document.getElementById('form-error'), status = document.getElementById('form-status');
    let busy = false, uncertain = false, attempt = null;
    function controls() {
      submit.disabled = busy || uncertain;
      check.disabled = busy;
      check.hidden = !uncertain;
      for (const input of form.querySelectorAll('input')) input.disabled = busy || uncertain;
    }
    function pending(text) { error.textContent = ''; status.hidden = false; status.textContent = text; }
    function navigate() { window.location.assign(form.dataset.next || '/'); }
    form.addEventListener('submit', async event => {
      event.preventDefault();
      if (busy || uncertain) return;
      const values = Object.fromEntries(new FormData(form));
      attempt = {email: values.email, id: null};
      let started = false;
      busy = true; pending(form.dataset.action === 'register' ? 'Создаём аккаунт…' : 'Выполняем вход…'); controls();
      try {
        await bounded(async signal => {
          const user = await mutate(form.dataset.action, values, signal, () => { started = true; });
          attempt.id = user.id;
          const current = await session(signal);
          if (!current || current.id !== user.id) throw problem('Вход не подтверждён. Проверьте состояние аккаунта.', {kind: 'invalid_response'});
        });
        navigate();
      } catch (failure) {
        uncertain = started && !failure.definitive;
        error.textContent = message(failure) + (uncertain ? ' Результат не подтверждён. Нажмите «Проверить вход» перед повтором.' : '');
      } finally {
        busy = false; status.hidden = true; controls();
        if (document.activeElement === document.body) (uncertain ? check : submit).focus();
      }
    });
    check.addEventListener('click', async () => {
      if (busy || !attempt) return;
      busy = true; pending('Проверяем состояние входа…'); controls();
      try {
        const current = await bounded(session);
        if (current && (attempt.id ? current.id === attempt.id : emailKey(current.email) === emailKey(attempt.email))) {
          navigate();
        } else if (current) {
          error.textContent = 'Сейчас выполнен вход в другой аккаунт. Обновите страницу, чтобы проверить его.';
        } else {
          uncertain = false;
          error.textContent = form.dataset.action === 'register'
            ? 'Сессия не активна. Если аккаунт уже создан, перейдите ко входу; иначе повторите регистрацию.'
            : 'Сессия не активна. Можно повторить вход.';
        }
      } catch (failure) { error.textContent = 'Не удалось проверить вход. ' + message(failure); }
      finally { busy = false; status.hidden = true; controls(); }
    });
  }

  const logout = document.getElementById('logout');
  if (logout) {
    const notice = document.getElementById('session-notice'), check = document.getElementById('logout-check');
    const error = document.getElementById('session-error'), status = document.getElementById('session-status');
    let busy = false;
    async function leave(checkOnly) {
      if (busy) return;
      busy = true; logout.disabled = true; check.disabled = true;
      notice.hidden = false; error.textContent = ''; status.hidden = false;
      status.textContent = checkOnly ? 'Проверяем выход из аккаунта…' : 'Выходим из аккаунта…';
      try {
        await bounded(async signal => {
          if (!checkOnly) {
            let started = false;
            try { await mutate('logout', undefined, signal, () => { started = true; }); }
            catch (failure) { if (!started) throw failure; }
          }
          const current = await session(signal);
          if (current) throw problem('Сессия ещё активна. Повторите выход или проверьте его состояние.');
        });
        window.location.assign('/');
      } catch (failure) {
        error.textContent = 'Выход не подтверждён. ' + message(failure);
        check.hidden = false;
      } finally {
        busy = false; logout.disabled = false; check.disabled = false; status.hidden = true;
        if (!check.hidden && document.activeElement === document.body) check.focus();
      }
    }
    logout.addEventListener('click', () => leave(false));
    check.addEventListener('click', () => leave(true));
  }
})();
