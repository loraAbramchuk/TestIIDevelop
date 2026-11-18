async function apiRequest(url, method, payload) {
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
    if (Array.isArray(data.detail)) throw new Error('Проверьте введённые данные.');
    throw new Error(data.detail || 'Не удалось выполнить запрос.');
  }
  return data;
}
