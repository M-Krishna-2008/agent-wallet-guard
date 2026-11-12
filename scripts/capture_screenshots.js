const { spawn } = require('child_process');
const fs = require('fs');

async function main() {
  const chrome = spawn('C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe', [
    '--headless=new',
    '--remote-debugging-port=9222',
    '--disable-gpu',
    'about:blank'
  ]);

  await new Promise(r => setTimeout(r, 1200));

  try {
    const listRes = await fetch('http://127.0.0.1:9222/json');
    const targets = await listRes.json();
    const target = targets.find(t => t.type === 'page');

    const ws = new WebSocket(target.webSocketDebuggerUrl);

    let id = 1;
    const callbacks = new Map();
    ws.onmessage = (msg) => {
      const data = JSON.parse(msg.data);
      if (data.id && callbacks.has(data.id)) {
        callbacks.get(data.id)(data);
        callbacks.delete(data.id);
      }
    };

    function send(method, params = {}) {
      return new Promise((resolve) => {
        const msgId = id++;
        callbacks.set(msgId, resolve);
        ws.send(JSON.stringify({ id: msgId, method, params }));
      });
    }

    await new Promise((r) => (ws.onopen = r));

    // Enable Page and Runtime
    await send('Page.enable');
    await send('Runtime.enable');

    // Navigate to landing page
    await send('Page.navigate', { url: 'http://127.0.0.1:8000/' });
    await new Promise(r => setTimeout(r, 1500));

    // 1. DESKTOP SCREENSHOT (1440px)
    await send('Emulation.setDeviceMetricsOverride', {
      width: 1440,
      height: 1200,
      deviceScaleFactor: 1,
      mobile: false,
    });
    await new Promise(r => setTimeout(r, 500));
    const desktopSnap = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync('static/screenshots/landing_desktop.png', Buffer.from(desktopSnap.result.data, 'base64'));
    console.log('Saved desktop screenshot (1440px)');

    // 2. MOBILE SCREENSHOT (390px)
    await send('Emulation.setDeviceMetricsOverride', {
      width: 390,
      height: 950,
      deviceScaleFactor: 2,
      mobile: true,
    });
    await new Promise(r => setTimeout(r, 500));

    // Check overflowing elements
    const evalRes = await send('Runtime.evaluate', {
      expression: `
        (() => {
          const elements = [];
          for (const el of document.querySelectorAll('*')) {
            if (el.scrollWidth > 390) {
              elements.push({
                tag: el.tagName,
                cls: el.className,
                scrollWidth: el.scrollWidth,
                clientWidth: el.clientWidth,
                rect: el.getBoundingClientRect()
              });
            }
          }
          return JSON.stringify({
            docScroll: document.documentElement.scrollWidth,
            bodyScroll: document.body.scrollWidth,
            overflows: elements.slice(0, 10)
          });
        })()
      `,
      returnByValue: true
    });
    console.log('Mobile overflow inspection:', evalRes.result.result.value);

    const mobileSnap = await send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync('static/screenshots/landing_mobile.png', Buffer.from(mobileSnap.result.data, 'base64'));
    console.log('Saved mobile screenshot (390px)');

    ws.close();
  } finally {
    chrome.kill();
  }
}

main().catch(err => console.error(err));

