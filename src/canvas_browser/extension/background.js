// Only the local Canvas command can request cookies, for one allowed website.
export async function cookieReply(message, api) {
  const allowed = api.runtime.getManifest().host_permissions[0].slice(0, -1);
  if (message.type !== 'cookies' || message.url !== allowed) {
    return { id: message.id, error: 'Website is not allowed' };
  }
  try {
    const cookies = await api.cookies.getAll({ url: allowed });
    const sameSite = { no_restriction: 'None', lax: 'Lax', strict: 'Strict' };
    return { id: message.id, cookies: cookies.map(cookie => ({
      name: cookie.name, value: cookie.value, domain: cookie.domain,
      path: cookie.path, expires: cookie.expirationDate ?? -1,
      httpOnly: cookie.httpOnly, secure: cookie.secure,
      sameSite: sameSite[cookie.sameSite] || 'Lax',
    })) };
  } catch {
    return { id: message.id, error: 'Chrome sign-in could not be read' };
  }
}

export function start(api) {
  let port;
  function connect() {
    if (port) return;
    const connection = api.runtime.connectNative('com.canvas.cli');
    port = connection;
    connection.onMessage.addListener(async message => {
      if (message.type !== 'cookies') return;
      const result = await cookieReply(message, api);
      if (port === connection) connection.postMessage(result);
    });
    connection.onDisconnect.addListener(() => {
      // Read the browser error without logging session data.
      void api.runtime.lastError;
      if (port === connection) port = undefined;
      api.alarms.create('connect-canvas', { delayInMinutes: 1 });
    });
  }
  api.runtime.onStartup.addListener(connect);
  api.runtime.onInstalled.addListener(connect);
  api.alarms.onAlarm.addListener(alarm => {
    if (alarm.name === 'connect-canvas') connect();
  });
  connect();
}

if (typeof chrome !== 'undefined') start(chrome);
