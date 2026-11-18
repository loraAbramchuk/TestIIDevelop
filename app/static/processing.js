function processingFailureMessage(code) {
  const messages = {
    unsupported_media: 'Формат видео не поддерживается. Выберите MP4 с видео H.264 (8 бит) и звуком AAC или без звука.',
    invalid_media: 'Не удалось прочитать видео. Проверьте файл или экспортируйте его заново в MP4.',
    video_dimensions_exceeded: 'Размер кадра превышает допустимый. Уменьшите разрешение видео.',
    video_frame_rate_exceeded: 'Частота кадров превышает допустимую. Уменьшите её перед загрузкой.',
    video_duration_exceeded: 'Длительность видео недопустима. Выберите непустой ролик в пределах лимита.',
    thumbnail_generation_failed: 'Не удалось создать превью. Попробуйте экспортировать видео заново.',
    source_missing_or_changed: 'Загруженный файл недоступен. Выберите файл и загрузите его повторно.',
    processing_budget_exceeded: 'Обработка превысила доступные ресурсы. Попробуйте файл меньшего размера или разрешения.',
    processing_timeout: 'Обработка заняла слишком много времени. Попробуйте более короткий ролик.',
    media_probe_timeout: 'Проверка файла заняла слишком много времени. Попробуйте экспортировать видео заново.',
    processing_storage_full: 'На сервере недостаточно места. Повторите загрузку позже.',
    processing_storage_unavailable: 'Хранилище временно недоступно. Повторите загрузку позже.',
    processing_database_unavailable: 'Сервис временно недоступен. Повторите загрузку позже.',
    processing_retries_exhausted: 'Не удалось завершить обработку после повторных попыток. Попробуйте позже.',
    processing_interrupted: 'Обработка была прервана. Попробуйте загрузить файл повторно.',
    upload_expired: 'Время загрузки истекло. Выберите файл и загрузите его повторно.',
  };
  return messages[code] || 'Загрузка или обработка завершилась ошибкой. Попробуйте другой файл или повторите позже.';
}

// One request at a time, bounded automatic polling, manual refresh after a pause.
function watchVideoStatus(videoId, render, reportError) {
  let timer = null, pending = null, controller = null;
  let polling = false, disposed = false, checks = 0, failures = 0, generation = 0;
  function stop() { polling = false; generation += 1; clearTimeout(timer); }
  async function refresh() {
    if (disposed) return null;
    if (pending) return pending;
    controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 10000);
    pending = (async () => {
      const response = await fetch(`/api/videos/${videoId}/status`, {
        credentials: 'same-origin', cache: 'no-store', signal: controller.signal,
      });
      if (!response.ok) {
        if ([401, 403, 404].includes(response.status)) stop();
        throw new Error('Не удалось проверить статус. Проверьте вход в аккаунт и доступ к видео.');
      }
      const video = await response.json();
      if (!disposed) {
        render(video);
        if (video.processing_status !== 'processing') stop();
      }
      return video;
    })();
    try { return await pending; }
    catch (failure) {
      if (disposed) return null;
      if (failure.name === 'AbortError') throw new Error('Проверка статуса заняла слишком много времени. Повторите попытку.');
      if (failure instanceof TypeError) throw new Error('Ошибка соединения. Повторите проверку статуса.');
      throw failure;
    }
    finally { clearTimeout(timeout); pending = null; }
  }
  async function tick() {
    if (!polling || disposed) return;
    const current = generation;
    checks += 1;
    try { await refresh(); failures = 0; }
    catch {
      failures += 1;
      if (!disposed) reportError('Не удалось обновить статус. Проверьте соединение или нажмите «Проверить статус».');
      if (failures >= 3) stop();
    }
    if (polling && !disposed && current === generation) {
      if (checks >= 600) {
        stop();
        reportError('Автопроверка приостановлена. Нажмите «Проверить статус», чтобы продолжить.');
      } else timer = setTimeout(tick, 3000);
    }
  }
  function start() {
    if (disposed) return;
    stop(); polling = true; checks = 0; failures = 0;
    timer = setTimeout(tick, 0);
  }
  function dispose() {
    disposed = true; stop(); controller?.abort();
    window.removeEventListener('pagehide', dispose);
  }
  window.addEventListener('pagehide', dispose, {once: true});
  return {refresh, start, dispose};
}
