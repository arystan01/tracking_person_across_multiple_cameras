import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { Simulator } from './Simulator'
import './styles.css'
import './console.css'
import './signin.css'

const Dashboard = new URLSearchParams(location.search).has('controls') ? App : Simulator
createRoot(document.getElementById('root')!).render(<StrictMode><Dashboard /></StrictMode>)
