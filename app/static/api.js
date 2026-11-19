function apiFailure(message, status, code) {
  return Object.assign(new Error(message), {status, code});
}

async function apiResponse(response, validate) {
  let data = null;
  if (response.status !== 204) {
    try { data = await response.json(); } catch (failure) { if (failure.name === 'AbortError') throw failure; }
  }
  const object = data && typeof data === 'object' && !Array.isArray(data);
  if (!response.ok) {
    if (object && Array.isArray(data.detail)) throw apiFailure('Проверьте введённые данные.', response.status);
    throw apiFailure(object && typeof data.detail === 'string' ? data.detail : 'Сервис временно недоступен. Попробуйте позже.', response.status, object ? data.code : undefined);
  }
  if ((response.status !== 204 && !object) || (validate && !validate(data))) {
    throw apiFailure('Не удалось подтвердить ответ сервера. Проверьте состояние перед повтором.', response.status, 'invalid_response');
  }
  return data;
}

// Bound both receiving headers and reading the response body. Never replay a write.
async function apiFetch(url, options, validate) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  try {
    return await apiResponse(await fetch(url, {...options, signal: controller.signal}), validate);
  } catch (failure) {
    if (controller.signal.aborted) {
      throw apiFailure('Превышено время ожидания ответа. Результат не подтверждён; проверьте сведения перед повтором.', 0, 'request_timeout');
    }
    throw failure;
  } finally { clearTimeout(timer); }
}

async function apiRead(url, validate) {
  return apiFetch(url, {credentials: 'same-origin', cache: 'no-store'}, validate);
}

async function apiRequest(url, method, payload, validate) {
  const csrf = await apiRead('/api/auth/csrf', data => typeof data?.csrf_token === 'string' && data.csrf_token.length > 0);
  return apiFetch(url, {
    method, credentials: 'same-origin', cache: 'no-store',
    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
    body: payload ? JSON.stringify(payload) : undefined,
  }, validate);
}
