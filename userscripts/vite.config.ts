import { defineConfig } from 'vite';
import monkey from 'vite-plugin-monkey';

export default defineConfig({
  plugins: [
    monkey({
      entry: 'src/main.ts',
      userscript: {
        name: 'Yanclaw Assistant',
        namespace: 'https://github.com/yanclaw',
        version: '1.0.0',
        description: 'Human-assisted crawler frontend for Yanclaw',
        match: ['*://*.edu.cn/*', '*://*.ac.cn/*'],
        grant: ['GM_xmlhttpRequest', 'GM_addStyle', 'GM_setValue', 'GM_getValue'],
        connect: ['127.0.0.1', 'localhost'],
      },
      build: {
        fileName: 'yanclaw-assistant.user.js',
      },
    }),
  ],
});
