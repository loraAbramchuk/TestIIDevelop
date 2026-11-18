// Validate a complete response before changing the page or confirming a write.
const ModerationData = (() => {
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const uuid = value => typeof value === 'string' && /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(value);
  const text = value => typeof value === 'string';
  const nullableText = value => value === null || text(value);
  const instant = value => text(value) && Number.isFinite(Date.parse(value));
  function video(value, id) {
    return object(value) && uuid(value.id) && value.id === id && uuid(value.owner_id) &&
      ['public', 'private'].includes(value.visibility) &&
      ['uploading', 'processing', 'ready', 'failed'].includes(value.processing_status) &&
      ['visible', 'hidden'].includes(value.moderation_status) && nullableText(value.moderation_reason) &&
      typeof value.deleted === 'boolean' && typeof value.can_watch === 'boolean' &&
      (value.can_watch ? text(value.title) && value.watch_url === `/watch/${id}` && !value.deleted &&
        value.processing_status === 'ready' && value.moderation_status === 'visible' : value.title === null && value.watch_url === null);
  }
  function receipt(value, videoId) {
    return object(value) && uuid(value.id) && uuid(value.video_id) && value.video_id === videoId &&
      ['spam', 'prohibited', 'copyright', 'other'].includes(value.reason) && text(value.explanation) &&
      ['open', 'resolved', 'dismissed'].includes(value.status) && instant(value.created_at);
  }
  function report(value, id) {
    return object(value) && receipt(value, value.video_id) && value.id === id && uuid(value.reporter_id) &&
      video(value.video, value.video_id) && (value.status === 'open' ? value.decided_at === null &&
        value.decided_by === null && value.decision_reason === null : instant(value.decided_at) &&
        (value.decided_by === null || uuid(value.decided_by)) && text(value.decision_reason) && value.decision_reason.length > 0);
  }
  function result(value, id) {
    return video(value, id) && typeof value.changed === 'boolean';
  }
  return {video, receipt, report, result};
})();
