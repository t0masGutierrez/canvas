import {readFile} from 'node:fs/promises';
import assert from 'node:assert/strict';
import test from 'node:test';

const source = await readFile(new URL('../src/canvas_browser/extension/background.js', import.meta.url));
const {cookieReply, start} = await import('data:text/javascript;base64,' + source.toString('base64'));
const fixture = {name:'canvas_session', value:'test-session', domain:'school.test',
  path:'/', secure:true, httpOnly:true, sameSite:'strict'};

function apiFor(cookies = [fixture]) {
  const calls = [];
  return {calls, runtime:{getManifest:() => ({host_permissions:['https://school.test/*']})},
    cookies:{getAll:async options => {calls.push(options); return cookies;}}};
}

test('reads current cookies for only the configured Canvas URL', async () => {
  const api = apiFor();
  assert.deepEqual(await cookieReply({id:'1', type:'cookies', url:'https://school.test/'}, api),
    {id:'1', cookies:[{name:'canvas_session', value:'test-session', domain:'school.test',
      path:'/', expires:-1, secure:true, httpOnly:true, sameSite:'Strict'}]});
  assert.deepEqual(api.calls, [{url:'https://school.test/'}]);
});

test('rejects another website before reading cookies', async () => {
  const api = apiFor();
  const result = await cookieReply({id:'2', type:'cookies', url:'https://other.test/'}, api);
  assert.equal(result.error, 'Website is not allowed');
  assert.deepEqual(api.calls, []);
});

test('rejects unknown operations', async () => {
  const api = apiFor();
  const result = await cookieReply({id:'3', type:'delete', url:'https://school.test/'}, api);
  assert.ok(result.error);
  assert.deepEqual(api.calls, []);
});

test('does not include cookie details in errors', async () => {
  const api = apiFor();
  api.cookies.getAll = async () => {throw new Error('private-test-value');};
  const result = await cookieReply({id:'4', type:'cookies', url:'https://school.test/'}, api);
  assert.equal(result.error, 'Chrome sign-in could not be read');
  assert.equal(JSON.stringify(result).includes('private-test-value'), false);
});

test('reconnects after the native host stops without duplicate connections', async () => {
  function event() {
    const handlers = [];
    return {addListener:fn => handlers.push(fn), emit:async data => {
      await Promise.all(handlers.map(fn => fn(data)));
    }};
  }
  const ports = [], alarms = [], sent = [];
  const api = apiFor();
  Object.assign(api.runtime, {onStartup:event(), onInstalled:event(), connectNative:name => {
    assert.equal(name, 'com.canvas.cli');
    const port = {onMessage:event(), onDisconnect:event(), postMessage:msg => sent.push(msg)};
    ports.push(port);
    return port;
  }});
  api.alarms = {onAlarm:event(), create:(name, opts) => alarms.push({name, ...opts})};
  start(api);
  await api.runtime.onStartup.emit();
  await api.runtime.onInstalled.emit();
  assert.equal(ports.length, 1);
  await ports[0].onMessage.emit({type:'ready'});
  assert.deepEqual(sent, []);
  await ports[0].onMessage.emit({id:'5', type:'cookies', url:'https://school.test/'});
  assert.equal(sent[0].cookies[0].value, 'test-session');
  await ports[0].onDisconnect.emit();
  assert.deepEqual(alarms, [{name:'connect-canvas', delayInMinutes:1}]);
  await api.alarms.onAlarm.emit({name:'connect-canvas'});
  assert.equal(ports.length, 2);
});
