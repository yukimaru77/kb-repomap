import fs from 'node:fs/promises';
import { pack, mergeConfigs, setLogLevel } from './node_modules/repomix/lib/index.js';

const [repo, work] = process.argv.slice(2);
setLogLevel('error');
const config = mergeConfigs(repo, {}, {
  output: {
    filePath: `${work}/repomix-packed.txt`,
    style: 'plain',
    git: { sortByChanges: false },
  },
});
const result = await pack([repo], config);
await fs.writeFile(
  `${work}/selected.json`,
  JSON.stringify(result.processedFiles.map(file => file.path).sort()),
);
