#!/usr/bin/env node
// Dispatch publication using the developer's existing gh authentication.
// Also copied into existing plugins: it needs neither a new SDK nor dependencies.
import { execFileSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

export function main(args = process.argv.slice(2)) {
  const run = (cmd, argv) => execFileSync(cmd, argv, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] }).trim();
  const fail = (message) => { throw new Error(message); };
  const root = run('git', ['rev-parse', '--show-toplevel']);
  const manifest = JSON.parse(readFileSync('plugin/manifest.json', 'utf8'));
  const repo = JSON.parse(run('gh', ['repo', 'view', '--json', 'nameWithOwner,defaultBranchRef']));
  const remote = repo.nameWithOwner;
  const branch = repo.defaultBranchRef?.name;
  if (!branch) fail('The GitHub repository has no default branch yet. Push your code first.');
  if (args[0] === 'register') {
    if (args.length !== 2) fail('Usage: npm run marketplace -- "Storage" (one existing category)');
    // Registration does not release code: it proposes the latest stable release.
    run('gh', ['workflow', 'run', 'register.yml', '--repo', 'Ervisio/plugins', '--ref', 'main',
      '-f', `id=${manifest.id}`, '-f', `repo=${remote}`, '-f', `category=${args[1]}`]);
    console.log('Registration queued. Review and merge the single PR containing the source and first release.');
    console.log('https://github.com/Ervisio/plugins/actions/workflows/register.yml');
    return;
  }
  const bump = args[0] ?? 'patch';
  if (!['patch', 'minor', 'major'].includes(bump)) fail('Usage: npm run release -- patch|minor|major [--notes "Release notes"]');
  let notes = '';
  if (args.length > 1) {
    if (args.length !== 3 || args[1] !== '--notes') fail('Only --notes "Release notes" is supported after the version bump.');
    notes = args[2];
  }
  if (run('git', ['status', '--porcelain'])) fail('Commit and push your changes before releasing.');
  const head = run('git', ['rev-parse', 'HEAD']);
  const latest = run('gh', ['api', `repos/${remote}/git/ref/heads/${encodeURIComponent(branch)}`, '--jq', '.object.sha']);
  if (head !== latest) fail(`Your checkout differs from ${remote}:${branch}. Merge and push your work first.`);
  const workflow = existsSync(join(root, '.github/workflows/release.yml')) ? 'release.yml' : 'plugin-release.yml';
  if (!existsSync(join(root, '.github/workflows', workflow))) fail('No release workflow found. Install the SDK template workflow first.');
  run('gh', ['workflow', 'run', workflow, '--repo', remote, '--ref', branch,
    '-f', `bump=${bump}`, '-f', `notes=${notes}`]);
  console.log(`Release queued for ${remote} (${bump}). Version, tests, changelog, tag and release run on GitHub.`);
  console.log(`https://github.com/${remote}/actions/workflows/${workflow}`);
  console.log('After publication: git pull --ff-only');
}

// Importable for tests; direct execution also works from npm scripts.
import { pathToFileURL } from 'node:url';
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try { main(); } catch (error) {
    console.error(`ervisio-plugin-release: ${error.stderr?.toString().trim() || error.message}`);
    process.exitCode = 1;
  }
}
