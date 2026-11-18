function apiFailure(message, status, code) {
  return Object.assign(new Error(message), {status, code});
}

async function apiRequest(url, method, payload) {
  const parse = async response => {
    try {
      const data = await response.json();
      if (data && typeof data === 'object' && !Array.isArray(data)) return data;
    } catch {}
    return {detail: 'Сервис временно недоступен. Попробуйте позже.'};
  };
  const csrfResponse = await fetch('/api/auth/csrf', {credentials: 'same-origin', cache: 'no-store'});
  const csrf = await parse(csrfResponse);
  if (!csrfResponse.ok || !csrf.csrf_token) throw apiFailure(typeof csrf.detail === 'string' ? csrf.detail : 'Обновите страницу и повторите попытку.', csrfResponse.status);
  const response = await fetch(url, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const data = response.status === 204 ? null : await parse(response);
  if (!response.ok) {
    if (Array.isArray(data.detail)) throw apiFailure('Проверьте введённые данные.', response.status);
    throw apiFailure(typeof data.detail === 'string' ? data.detail : 'Не удалось выполнить запрос.', response.status, data.code);
  }
  return data;
}
