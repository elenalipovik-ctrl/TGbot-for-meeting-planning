import {readFile,writeFile} from 'node:fs/promises';
const schema=JSON.parse(await readFile(new URL('../docs/mini_app_openapi.json',import.meta.url),'utf8'));
function type(s){
 if(s.$ref)return s.$ref.split('/').at(-1);
 if(s.const!==undefined)return JSON.stringify(s.const);
 if(s.enum)return s.enum.map(v=>JSON.stringify(v)).join(' | ');
 if(s.anyOf||s.oneOf)return (s.anyOf||s.oneOf).map(type).join(' | ');
 if(s.type==='null')return 'null';
 if(s.type==='string')return 'string';
 if(s.type==='integer'||s.type==='number')return 'number';
 if(s.type==='boolean')return 'boolean';
 if(s.type==='array')return '('+type(s.items||{})+')[]';
 if(s.type==='object'||s.properties){
  if(s.properties)return '{ '+Object.entries(s.properties).map(([k,v])=>JSON.stringify(k)+(s.required?.includes(k)?'':'?')+': '+type(v)).join('; ')+' }';
  return 'Record<string, '+(s.additionalProperties&&typeof s.additionalProperties==='object'?type(s.additionalProperties):'unknown')+'>';
 }
 return 'unknown';
}
const content='// Generated from docs/mini_app_openapi.json. Do not edit.\n'+Object.entries(schema.components.schemas).map(([name,s])=>'export type '+name+' = '+type(s)+';').join('\n')+'\n';
const target=new URL('../mini_app/src/generated/api.ts',import.meta.url);
if(process.argv.includes('--check')){if(await readFile(target,'utf8')!==content)throw new Error('API types are out of date');}else await writeFile(target,content);
