import numpy as np
import exoplanet as xo
import lightkurve as lk
import pymc3 as pm
import pymc3_ext as pmx
import arviz as az
from scipy.signal import medfilt
from astropy.stats import sigma_clip
import io_utils
import plot_utils

#TODO: a to_latex function that takes the arviz summary output and generates
#table-ready latex strings

def clean_up(x, ys, yerrs, compars, weight_guess, cutoff_frac = 0.,
	end_num = 0, medfilt_kernel = 11, sigma_cut = 5):

	#n sigma outlier rejection against a median filter
	full_mask = np.ones(x.shape, dtype = 'bool')
	quick_detrend = ys[0]/weight_guess.dot(compars)
	median_filter = medfilt(quick_detrend, medfilt_kernel)
	filt = quick_detrend / median_filter
	masked_arr = sigma_clip(filt, sigma = sigma_cut)
	full_mask = ~masked_arr.mask

	#flux cutoff for very rapidly varying light curve
	cutoff = cutoff_frac*max(ys[0])
	mask = ys[0] > cutoff
	full_mask &= mask

	#loosing the last few data
	mask = np.ones(x.shape, dtype = 'bool')
	if end_num > 0:
		mask[-end_num:] = False
	full_mask &= mask

	n_reject = sum(~full_mask)
	clip_perc = n_reject/len(full_mask)*100.
	print(f"Clipped {clip_perc}% of the data")

	return x[full_mask], ys[:,full_mask], yerrs[:,full_mask], \
		compars[:, full_mask], full_mask

def quick_aperture_optimize(dump_dir, plot_dir, apertures,
	flux_cutoff = 0., end_num = 0, filter_width = 31):
	print("Running quick aperture optimization...")
	rmses = []
	for i in apertures:
		x, ys, yerrs, _, _, _, _, _ = \
			io_utils.load_phot_data(dump_dir, i)
		compars= ys[1:]
		weight_guess = np.array([0.5]*len(compars))

		x, ys, yerrs, compars, _ = clean_up(
			x, ys, yerrs, compars, weight_guess,
			cutoff_frac = flux_cutoff, end_num = end_num)

		quick_detrend = ys[0]/weight_guess.dot(compars)
		median_filter = medfilt(quick_detrend, filter_width)
		filt = quick_detrend / median_filter
		rmses.append(np.std(filt)/len(x))

	plot_utils.plot_aperture_opt(plot_dir, apertures, rmses)
	best_ap = apertures[np.argmin(rmses)]
	print(f"Complete! Optimal aperture is {best_ap} pixels.")

	return best_ap

def get_covariates(bkgs_init, centroid_x_init, centroid_y_init, airmass, widths,
	background_mode, mask):

	d_from_med_init = gen_d_from_med(centroid_x_init, centroid_y_init)

	if background_mode == 'helium':
		background = None
		water_proxy = gen_water_proxy(bkgs_init)[mask]
	else:
		water_proxy = None
		background = bkgs_init[mask]
	
	covariate_dict = {
			'x_cent': np.array(
				centroid_x_init[0][mask],dtype = float),
			'y_cent': np.array(
				centroid_y_init[0][mask],dtype = float),
			'd_from_med': np.array(
				d_from_med_init[mask],dtype=float),
			'water_proxy': np.array(water_proxy,dtype = float),
			'airmass' : np.array(airmass[mask],dtype = float),
			'psf_width' : np.array(widths[0][mask], dtype = float),
			'background' : np.array(background, dtype = float)}
	return covariate_dict

def crossmatch_covariates(covariates, covariate_dict):
	return [covariate_dict[cov] for cov in covariates]

def fit_lightcurve(dump_dir, plot_dir, best_ap, background_mode,
	covariate_names,  texp, r_star_prior, t0_prior, period_prior,
	a_rs_prior, b_prior, ror_prior, jitter_prior, tune = 1000, 
	draws = 1500, target_accept = 0.99, phase = 'primary',
	ldc_val = None, flux_cutoff = 0., end_num = 0):
	
	x_init, ys_init, yerrs_init, bkgs_init, centroid_x_init, \
		centroid_y_init, airmass, widths = \
		io_utils.load_phot_data(dump_dir, best_ap)
	compars_init = ys_init[1:]
	weight_guess_init = np.array([0.5]*len(compars_init))

	x, ys, yerrs, compars, mask = clean_up(x_init, ys_init, yerrs_init,
		compars_init, weight_guess_init, flux_cutoff, end_num)	

	cov_dict = get_covariates(bkgs_init, centroid_x_init, centroid_y_init,
		airmass, widths, background_mode, mask)
	covs = crossmatch_covariates(covariate_names, cov_dict)
	plot_utils.plot_quickfit(plot_dir, x, ys, yerrs)
	plot_utils.plot_covariates(plot_dir, x, covariate_names, covs)

	weight_guess = np.array([0.5]*compars.shape[0] + [0]*len(covs)) 
	compars = np.vstack((compars, *covs))
		
	print("Constructing model...")

	##model in pymc3
	model, map_soln = make_model(x, ys, yerrs, compars, weight_guess,
		texp, r_star_prior, t0_prior, period_prior,
		a_rs_prior, b_prior, ror_prior, jitter_prior, phase, ldc_val)
	plot_utils.plot_initial_map(plot_dir, x, ys, yerrs, compars, map_soln)
	print("Initial MAP found!")
	print("Sampling posterior...")
	trace = sample_model(model, map_soln, tune, draws, target_accept)
	trace.to_netcdf(f'{dump_dir}posterior.nc')
	print("Sampling complete!")
	new_map = get_new_map(trace)
	summary, varnames = gen_summary(plot_dir, trace, phase, ldc_val)
	print("Making corner and trace plots...")
	plot_utils.corner_plot(plot_dir, trace, varnames)
	plot_utils.trace_plot(plot_dir, trace, varnames)
	print("Visualizing fit...")
	plot_utils.tripleplot(plot_dir, dump_dir, x, ys, yerrs, compars,
		new_map, trace, phase = phase)
	print("Fitting complete!")
	return None	

def gen_summary(plot_dir, trace, phase, ldc_val):
	if phase == 'primary':
		varnames = ['t0', 'period', 'a_rs', 'b', 'ror',
			'jitter', 'baseline', 'weights']
	else:
		varnames = ['t_second', 'period', 'a_rs', 'b', 'fpfs',
			'r_star', 'jitter', 'baseline', 'weights']
	if ldc_val is None:
		varnames += ['u']
	
	func_dict = {
		"16%": lambda x: np.percentile(x, 16),
		"50%": lambda x: np.percentile(x, 50),
		"84%": lambda x: np.percentile(x, 84),
		"95%": lambda x: np.percentile(x, 95)
	}

	summary = az.summary(trace, var_names = varnames,
		stat_funcs = func_dict, round_to = 16,
		kind = 'all')
	summary.to_csv(f'{plot_dir}fit_summary.csv')

	return summary, varnames

def get_new_map(trace):
	dat = np.array(trace.sample_stats.lp)
	ind = np.unravel_index(dat.argmax(), dat.shape)
	new_map = trace.posterior.isel(chain=ind[0], draw = ind[1])
	return new_map

def sample_model(model, map_soln, tune, draws, target_accept):
	with model:
		trace = pmx.sample(
			tune=tune,
			draws=draws,
			start=map_soln,
			return_inferencedata = True,
			target_accept=target_accept
		)
		return trace


def unpack_prior(name, prior_tuple):
	func_dict = {'normal': pm.Normal,
		'uniform': pm.Uniform}
	func, a, b = prior_tuple
	return func_dict[func](name, a, b)

def make_model(x, ys, yerrs, compars, weight_guess, texp, r_star_prior,
	t0_prior, period_prior, a_rs_prior, b_prior, ror_prior,
	jitter_prior, phase = 'primary', ldc_val = None):
	##currently doing circular orbits ONLY

	with pm.Model() as model:
		if ldc_val:
			star = xo.LimbDarkLightCurve(ldc_val)
		else:
			u = xo.distributions.QuadLimbDark("u")
			star = xo.LimbDarkLightCurve(u)
		r_star = unpack_prior('r_star', r_star_prior)

		period = unpack_prior('period', period_prior)
		t0 = unpack_prior('t0', t0_prior)
		if phase == 'primary':
			t = t0
		else:
			t = pm.Deterministic("t_second", t0 + period/2)

		a_rs = unpack_prior('a_rs', a_rs_prior)
		b = unpack_prior('b', b_prior)
		if phase == 'primary':
			ror = unpack_prior('ror', ror_prior)
		else:
			fpfs = unpack_prior('fpfs', fpfs_prior)
			ror = np.sqrt(fpfs)

		orbit = xo.orbits.KeplerianOrbit(period = period,
			t0 = t, b = b, a = a_rs*r_star, r_star = r_star)
		#lightcurve
		lightcurve = pm.Deterministic("light_curve", pm.math.sum(
			star.get_light_curve(orbit=orbit, r = ror*r_star,
			t = x, texp = texp), axis = -1) + 1.)

		#systematics
		comp_weights = pm.Uniform("weights", lower = -1., upper = 1.,
			testval = weight_guess, shape = len(weight_guess))
		systematics = pm.math.dot(comp_weights, compars)

		#baseline
		vec = x - np.median(x)
		base = pm.Uniform(f"baseline", -1, 1., shape = 2,
			testval = [0., 0.])
		baseline = base[0]*vec + base[1]

		jitter = unpack_prior('jitter', jitter_prior)
		full_model = baseline + systematics*lightcurve
		full_variance = yerrs[0]**2 + jitter**2

		pm.Normal(f"obs", mu=full_model, sd=np.sqrt(full_variance),
			observed=ys[0])

		map_soln = model.test_point
		map_soln = pmx.optimize(map_soln, [comp_weights,
			baseline, jitter])
		map_soln = pmx.optimize(map_soln)

		return model, map_soln

def gen_water_proxy(bkgs):
	oh_2 = np.mean(bkgs[:,72:89], axis = 1) 
	oh_3 = np.mean(bkgs[:,180:190], axis = 1)
	oh_4 = np.mean(bkgs[:,201:210], axis = 1)
	
	emission_proxy = (oh_3 +oh_4)/2
	absorption_proxy = oh_2/emission_proxy
	absorption_proxy /= np.median(absorption_proxy)
	return absorption_proxy

def gen_d_from_med(centroid_x_init, centroid_y_init):
	med_x = np.median(centroid_x_init[0])
	med_y = np.median(centroid_y_init[0])
	d_from_med_init = np.sqrt((centroid_x_init[0] - med_x)**2 \
		+ (centroid_y_init[0] - med_y)**2)
	return d_from_med_init
