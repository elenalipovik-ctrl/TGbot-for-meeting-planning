import {defineConfig} from 'vite';
export default defineConfig({base:'/app/',build:{target:'es2022'},server:{host:'127.0.0.1',port:5173,strictPort:true,proxy:{'/api':'http://127.0.0.1:8766','/local-preview':'http://127.0.0.1:8766'}}});
