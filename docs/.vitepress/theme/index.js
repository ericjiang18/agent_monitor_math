// Register the Ansatz fonts, styles, mobile sign-in link, and institution rail.
import { h, Fragment } from 'vue'
import DefaultTheme from 'vitepress/theme-without-fonts'
import '@fontsource-variable/newsreader/wght.css'
import '@fontsource-variable/space-grotesk/wght.css'
import InstitutionRail from './InstitutionRail.vue'
import ResearchDag from './ResearchDag.vue'
import OpenProblems from './OpenProblems.vue'
import HeroMotion from './HeroMotion.vue'
import HomeReveal from './HomeReveal.vue'
import './custom.css'

export default {
  extends: DefaultTheme,
  enhanceApp({ app }) {
    app.component('ResearchDag', ResearchDag)
    app.component('OpenProblems', OpenProblems)
    if (typeof window === 'undefined') return

    const root = document.documentElement
    const themeColor = document.querySelector('meta[name="theme-color"]')
    const syncThemeColor = () => {
      themeColor?.setAttribute('content', root.classList.contains('dark') ? '#111318' : '#ffffff')
    }

    syncThemeColor()
    new MutationObserver(syncThemeColor).observe(root, {
      attributes: true,
      attributeFilter: ['class']
    })
  },
  Layout: () => h(DefaultTheme.Layout, null, {
    'home-hero-image': () => h(HeroMotion),
    'home-hero-info-before': () => h('p', { class: 'ansatz-hero-kicker' }, [
      h('span', { 'aria-hidden': 'true' }),
      'UCLA · AI + MATHEMATICS'
    ]),
    'nav-bar-content-after': () => h('a', {
      class: 'ansatz-mobile-signin',
      href: '/sign-in',
      'aria-label': 'Sign in to Ansätze'
    }, 'Sign in'),
    'home-hero-after': () => h(Fragment, [h(InstitutionRail), h(HomeReveal)])
  })
}
