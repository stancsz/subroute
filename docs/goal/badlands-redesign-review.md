# Independent dashboard and branding acceptance review

Verdict: READY for the reviewed frontend and isolated publication artifact scopes. Publication/readback remains the builder's responsibility. No remaining confirmed acceptance blocker in this scope.

I did not author product files. I inspected applicable repository instructions, the codex-qa skill, goal, current frontend source, actual Chrome-rendered fixture, and the company release artifact. Only disposable fixture policy/state/flags and ignored QA evidence were written. No real configuration, credentials, deployment or inference calls were used.

## Actual independent tasks

- Target OpenAI/codex-luna/high and advisor Gemini/gemini-subscription-pro/low were saved and reloaded exactly (fixture policy v8). Failed save restored the previous selections and left persisted state identical; final red chip says Not saved / previous policy remains active.
- Initial offline load disables editing and renders Policy unavailable for request policy, rather than a guessed alias. Final Refresh recovers exact saved v8. Quota failure retains seven configured providers and explicit unavailable measurements. Successful empty inventory shows zero configured sources with setup guidance. Restoring flags recovers seven sources. Flags now {}.
- Desktop fixture covers installed-ready Codex, installed-failed Claude, missing OpenCode. Check again refreshes the failed CLI state; Choose folder & launch invokes the simulated chooser then launch in D:/fixture-project. Final refinement changes the button to Launch after selection. Install shows the simulated installer result. This is bridge simulation, not a real native application/CLI launch.
- Desktop connection page without a native bridge handles Connect with an explicit desktop-required message and reenabled control, preserving the loopback default.
- At CSS 390px Routing top/end and Connections top/end are readable without horizontal body overflow. Navigation closes after selection, focuses page-title and scrolls to top. Escape returns focus to menu-toggle. More connections expands five unavailable providers, with clear unsupported statuses.
- At actual fractional viewport 767.5px the original gap was reproduced independently from settled DOM: four-column footer clipped outside its row. Repaired CSS uses width < 768 and stacked rows through 1000. Recheck: sidebar hidden, mobile true, card scrollWidth=clientWidth=714 and footerRight715.51<cardRight731.50. At 768.75px rows also stack and details remain contained. Saved desktop and mobile pixels inspected.
- Final unsaved provider choice reads Choose a target model to save this provider in both policy chip and status; reload restores saved OpenAI/Luna/v8 without any pending choice save.
- Mesa B logo pixels inspected on Subroute Routing, Connections footer, desktop connection, company header, gateway console and phone signin. Parent proof images at DPR1 and DPR2 were independently inspected for black/white 16/20/24/32px mark and lockup legibility. Geometry is consistent across source surfaces.

## Evidence and limits

Own screenshots are [view](../brand/proofs/qa-company-wide.jpg), [view](../brand/proofs/qa-connections-7675-fixed.jpg), [view](../brand/proofs/qa-routing-390-top.jpg), [view](../brand/proofs/qa-routing-390-end.jpg), [view](../brand/proofs/qa-save-failure-final.jpg), [view](../brand/proofs/qa-offline-final.jpg), [view](../brand/proofs/qa-desktop-retry-launch-final.jpg), [view](../brand/proofs/qa-more-connections-wide.jpg), [view](../brand/proofs/qa-more-connections-390-end.jpg), [view](../brand/proofs/qa-connections-390-top.jpg), [view](../brand/proofs/qa-connections-768-end.jpg), [view](../brand/proofs/qa-signin-logo-390.jpg), [view](../brand/proofs/qa-desktop-connect-logo.jpg), and [view](../brand/proofs/qa-gateway-logo-static.jpg) in the linked proof directory. Final button text refinement was verified by AX after these desktop screenshots.

Chrome screenshots initially timed out when the tab was in the background; Page.bringToFront enabled normal screenshots. Native select popup appearance remains UNVERIFIED: own Chrome click/Alt+Down and screenshots did not show an OS popup. Native option selection, accessibility options, fixture saves and persistence were tested. Do not represent the saved popup-attempt image as an expanded menu.

The 64921 fixture uses actual frontend files with synthetic API responses and a native bridge stub, and labels illustrative quota values. Gateway console at this fixture has no management API and correctly displays unavailable data; it establishes logo appearance only. Prior independent console CRUD/failure/dialog acceptance is separate evidence. No authenticated production console journey was repeated for branding-only changes.

## Publication artifact scope

Independent comparisons of company release-assets against dist-site found exactly seven changed assets: index, start-project, docs/api, 404, site.css, favicon.svg and badlands-mark.svg. All four HTML files equal the captured live HTML plus approved logo transforms; script-src and form tags are unchanged. CSS equals live CSS plus only the logo append. admin.html and console.js remain exact prior dist bytes. Retained Worker SHA256 is 494ede1a430e05c36b232bb8f629639a7a866f757477b3acf002b48f07b5d953. Prepared config retains ASSETS, ADMIN_LOGIN_LIMIT5/60 and existing routes, with no credential values. The separate gateway console Worker must publish its own rebuilt assets; the company artifact intentionally retains opaque unused old gateway assets. This review is not a fresh authenticated readback of that deployment or proof of no unrelated bugs.

## Exact reviewed source hashes

- `D:\github\subroute\src\subroute\control\index.html`: `106625aa282ae03feeca138ba0b6166a23565007b8e1259debfc3bbefa7974be`
- `D:\github\subroute\src\subroute\control\styles.css`: `1a8bcbb09e775d39be4b2ff9dac6a7b6cfd4317e6285c29f6e05f47defaf1b11`
- `D:\github\subroute\src\subroute\control\app.js`: `b85eb4afd00202b25c3e76d65c3ca5e3426a697ec5bd57b46a4bbf88436e35b3`
- `D:\github\subroute\desktop\connect.html`: `44b1c7c0c4eff51762f03850e13bf83c5df2aa6fddca37d24f00c166d6cc261a`
- `D:\github\subroute\desktop\connect.css`: `2f4ee1aac27dceefe4a00e96233f93fe31dd6d2721ef9b3b9d6c6f9acc8955a3`
- `D:\github\badlands-labs-site\site\gateway.html`: `5ae8baafdde77cf6c4275200d67dd82b693c7a5d4074db0f9b5b5dbbbf2c6772`
- `D:\github\badlands-labs-site\site\gateway\console.css`: `26e339ee2e2b41b065823620c788c57fdaa3483e156e2e0077ba8f5469b7f4c9`
- `D:\github\badlands-labs-site\site\gateway\signin.html`: `aae140db31551768d797c09dce5aa4c8ef74181c755084ba64e2f7f476dc6893`
- `D:\github\badlands-labs-site\site\gateway\signin.css`: `c1e5cde3b0654853fb9c9d2bc77910017053cce665e2be81d6f5e55668f98d05`
- `D:\github\badlands-labs-site\site\index.html`: `e326000ba2670ade8b5c9e44316fb692dab53962d507f5133f5d080951b9462e`
- `D:\github\badlands-labs-site\site\site.css`: `1b671103cac75c64dc7fe9dd88fc00854a3a625ebc73dd9db1fe2886f63b4632`
- `D:\github\badlands-labs-site\site\start-project.html`: `f74daea5689b5ed0bd9f695c9354f4db78e6f4bf90bd030aeb687e355ed9a5fd`
- `D:\github\badlands-labs-site\site\badlands-mark.svg`: `ced826c707e039a9494c0f4912eb394464d2715b335a446fc50eaf51ce87992b`
- `D:\github\badlands-labs-site\site\favicon.svg`: `5bcb8d6002e3a4ec6ce69a8e6e419fe6b54f0e0eb64506cf1e5e122db13c09df`

## Post-publication independent readback

Scoped publication verification: READY. Final clean directory configuration has find_additional_modules:false, no_bundle:true and one named main file. Its Worker bytes equal the earlier reviewed source with SHA256494ede1a430e05c36b232bb8f629639a7a866f757477b3acf002b48f07b5d953; all assets and asset-file sets exactly equal the reviewed release artifact. Parent-run authenticated API verifier explicitly asserts exactly one module, unchanged source, bindings and compatibility settings, and its successful safe JSON evidence was inspected. I did not rerun authenticated API verification or read credential files; deployed module-count evidence is that parent-run assertion, not an independently authenticated measurement.

Independent unauthenticated HTTP readback matched exact reviewed bytes for all seven public assets, with expected200/404 status and MIME type. /admin returns401; /gateway returns302 Location /gateway/signin; signin200 contains Mesa branding. Signin HTML/CSS/JS match the separate dist-gateway published artifact exactly. Actual production company and signin pixels were independently inspected and saved as [view](../brand/proofs/qa-company-published.jpg) and [view](../brand/proofs/qa-signin-published.jpg). No login, form submission, provider inference, routing or account change was performed. Readback metadata is qa-publication-readback.json and qa-signin-publication-bytes.json. Parent identifies final versions company6b255578-420f-4bf1-b3ec-71a0d59f31df and consoleeef1279f-ef3e-42b7-891b-6fa4eeea22d3; those version IDs were not independently read from an authenticated API.
