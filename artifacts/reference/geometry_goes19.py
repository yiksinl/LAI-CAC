import numpy as np
import netCDF4 as nc
import matplotlib.pyplot as plt

import datetime

def calculate_solar_angles(lat, lon, utc_time):
    """
    Calculates the Solar Zenith Angle (SZA) and Solar Azimuth Angle (SAA) 
    for a 2D grid of latitudes and longitudes at a specific UTC time.
    
    Based on the vectorized NOAA ESRL Solar Position Calculator.
    Azimuth convention: North = 0°, East = 90°, South = 180°, West = 270°.
    """
    # 1. --- Compute Julian Day ---
    year = utc_time.year
    month = utc_time.month
    day = utc_time.day
    
    if month <= 2:
        year -= 1
        month += 12
        
    A = year // 100
    B = 2 - A + (A // 4)
    
    # Calculate base Julian Day and add fractional day
    jd_base = int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + B - 1524.5
    frac_day = (utc_time.hour + utc_time.minute / 60.0 + utc_time.second / 3600.0) / 24.0
    jd = jd_base + frac_day
    
    # 2. --- Compute Orbital Elements (Scalars) ---
    # Because the time is constant for the whole grid, these are calculated once.
    jc = (jd - 2451545.0) / 36525.0

    geom_mean_long_sun = (280.46646 + jc * (36000.76983 + jc * 0.0003032)) % 360.0
    geom_mean_anom_sun = 357.52911 + jc * (35999.05029 - 0.0001537 * jc)
    eccent_earth_orbit = 0.016708634 - jc * (0.000042037 + 0.0000001267 * jc)

    m_rad = np.radians(geom_mean_anom_sun)
    
    # Equation of Center
    sun_eq_of_ctr = (np.sin(m_rad) * (1.914602 - jc * (0.004817 + 0.000014 * jc)) + 
                     np.sin(2.0 * m_rad) * (0.019993 - 0.000101 * jc) + 
                     np.sin(3.0 * m_rad) * 0.000289)

    sun_true_long = geom_mean_long_sun + sun_eq_of_ctr
    sun_app_long = sun_true_long - 0.00569 - 0.00478 * np.sin(np.radians(125.04 - 1934.136 * jc))

    mean_obliq_ecliptic = 23.0 + (26.0 + ((21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813)))) / 60.0) / 60.0
    obliq_corr = mean_obliq_ecliptic + 0.00256 * np.cos(np.radians(125.04 - 1934.136 * jc))

    # Solar Declination (Angle of the sun relative to the equator)
    declination_rad = np.arcsin(np.sin(np.radians(obliq_corr)) * np.sin(np.radians(sun_app_long)))
    
    # Equation of Time (Correction for the Earth's elliptical orbit, in minutes)
    var_y = np.tan(np.radians(obliq_corr / 2.0)) ** 2
    eq_time = 4.0 * np.degrees(var_y * np.sin(2.0 * np.radians(geom_mean_long_sun)) - 
              2.0 * eccent_earth_orbit * np.sin(m_rad) + 
              4.0 * eccent_earth_orbit * var_y * np.sin(m_rad) * np.cos(2.0 * np.radians(geom_mean_long_sun)) - 
              0.5 * var_y ** 2 * np.sin(4.0 * np.radians(geom_mean_long_sun)) - 
              1.25 * eccent_earth_orbit ** 2 * np.sin(2.0 * m_rad))
              
    # 3. --- Compute Grid Geometry (2D Arrays) ---
    lat_rad = np.radians(lat)
    
    # Total minutes past midnight UTC
    time_min = utc_time.hour * 60.0 + utc_time.minute + utc_time.second / 60.0
    
    # True Solar Time (minutes) adjusting for longitude and Equation of Time
    tst = (time_min + eq_time + 4.0 * lon) % 1440.0
    
    # Hour Angle (degrees): Solar noon is 0°, morning is negative, afternoon is positive
    ha_deg = (tst / 4.0) - 180.0
    ha_rad = np.radians(ha_deg)

    # 4. --- Calculate Solar Zenith Angle (SZA) ---
    cos_sza = (np.sin(lat_rad) * np.sin(declination_rad) + 
               np.cos(lat_rad) * np.cos(declination_rad) * np.cos(ha_rad))
    cos_sza = np.clip(cos_sza, -1.0, 1.0)
    sza = np.degrees(np.arccos(cos_sza))

    # 5. --- Calculate Solar Azimuth Angle (SAA) ---
    denominator = np.cos(lat_rad) * np.sin(np.radians(sza))
    # Safeguard against division by zero at the exact poles or exact sub-solar point
    denominator = np.where(np.abs(denominator) < 1e-6, 1e-6, denominator)
    
    cos_saa = (np.sin(lat_rad) * cos_sza - np.sin(declination_rad)) / denominator
    cos_saa = np.clip(cos_saa, -1.0, 1.0)
    saa_calc = np.degrees(np.arccos(cos_saa))

    # Quadrant correction: Azimuth is measured clockwise from North
    saa = np.where(ha_deg > 0, (360.0 - saa_calc) % 360.0, saa_calc % 360.0)
    
    # Mask invalid/space pixels (where lat/lon are NaN)
    invalid = np.isnan(lat) | np.isnan(lon)
    sza[invalid] = np.nan
    saa[invalid] = np.nan

    return sza, saa

def calculate_abi_angles_from_latlon(lat, lon, lon0=-75.0):
    """
    Calculates GOES VZA and VAA directly from Latitude and Longitude grids.
    Defaults to lon0 = -75.0 for GOES-East. Change to -137.0 for GOES-West.
    """
    # --- GOES-R Standard Projection Parameters ---
    r_major = 6378137.0
    r_minor = 6356752.31414
    H = 42164160.0        # Perspective Point Height + r_major
    e = 0.0818191910435   # Earth Eccentricity
    lat0 = 0.0            # Geostationary satellite latitude is always the equator
    rad2deg = 180.0 / np.pi
    
    # Convert inputs to radians
    lat_rad = lat / rad2deg
    d_lon = (lon - lon0) / rad2deg
    
    # 1. Calculate Geocentric Latitude (fc)
    fc = np.arctan(((r_minor / r_major)**2) * np.tan(lat_rad))
    
    # 2. Calculate Earth radius at this specific latitude (r_c)
    r_c = r_minor / np.sqrt(1.0 - (e**2) * (np.cos(fc)**2))
    
    # 3. Calculate exact distance from the satellite to the surface pixel (r_s)
    # Using the Law of Cosines on the Earth-Satellite-Pixel triangle
    r_s = np.sqrt(H**2 + r_c**2 - 2.0 * H * r_c * np.cos(fc) * np.cos(d_lon))
    
    # 4. Calculate Satellite Zenith Angle (VZA)
    acos_arg = (r_c**2 + r_s**2 - H**2) / (2.0 * r_c * r_s)
    acos_arg = np.clip(acos_arg, -1.0, 1.0)
    vza = 180.0 - (np.arccos(acos_arg) * rad2deg)
    
    # 5. Calculate Satellite Azimuth Angle (VAA)
    tmp_dist = np.sqrt((r_c * np.cos(fc))**2 + H**2 - 2.0 * H * r_c * np.cos(fc))
    
    asin_arg = H * np.sin(np.abs(d_lon)) / tmp_dist
    asin_arg = np.clip(asin_arg, -1.0, 1.0)
    vaa = np.arcsin(asin_arg) * rad2deg
    
    # Quadrant corrections for Azimuth
    mask1 = (lon < lon0) & (lat > lat0)
    vaa[mask1] = 180.0 - vaa[mask1]
    
    mask2 = (lon > lon0) & (lat > lat0)
    vaa[mask2] = 180.0 + vaa[mask2]
    
    mask3 = (lon > lon0) & (lat < lat0)
    vaa[mask3] = 360.0 - vaa[mask3]
    
    # Mask invalid/space pixels (where lat/lon are NaN or fill values)
    invalid = np.isnan(lat) | np.isnan(lon)
    vza[invalid] = np.nan
    vaa[invalid] = np.nan
    
    return vza, vaa

# --- Execution and Plotting Block ---
if __name__ == '__main__':
    file_path = r'F:\GOES\NAV\GOES_Navigation_2kmFD-GOES-East.nc'
    
    print(f"Reading NetCDF file: {file_path}")
    with nc.Dataset(file_path, 'r') as f:
        # Load variables. 
        # Using [:] forces them into numpy arrays and unpacks any scale factors.
        lat_data = f.variables['Latitude'][:]
        lon_data = f.variables['Longitude'][:]
        vza_local = f.variables['LocalZenithAngle'][:]
        
        # Ensure fill values (space pixels) are treated as NaNs for accurate math
        if hasattr(lat_data, 'filled'):
            lat_data = lat_data.filled(np.nan)
        if hasattr(lon_data, 'filled'):
            lon_data = lon_data.filled(np.nan)
        if hasattr(vza_local, 'filled'):
            vza_local = vza_local.filled(np.nan)

    # ... (Keep the data loading exactly the same) ...

    # FIX 1: Change lon0 to -75.2 for accurate GOES-East (GOES-16/19) geometry
    print("Calculating angles directly from Latitude and Longitude...")
    vza_calc, vaa_calc = calculate_abi_angles_from_latlon(lat_data, lon_data, lon0=-75.2)
    
    print("Calculating differences...")
    diff_vza = vza_calc - vza_local
    
    # Print statistics
    print(f"Mean Error: {np.nanmean(diff_vza):.6e}°")
    print(f"Max Abs Error: {np.nanmax(np.abs(diff_vza)):.6e}°")
    
    # Plotting
    print("Generating difference map...")
    plt.figure(figsize=(10, 8))
    
    max_diff = np.nanmax(np.abs(diff_vza))
    v_lim = 1e-3 if (np.isnan(max_diff) or max_diff == 0) else max_diff

    im = plt.imshow(diff_vza, cmap='coolwarm', vmin=-v_lim, vmax=v_lim, origin='upper')
    
    cbar = plt.colorbar(im, fraction=0.046, pad=0.04)
    cbar.set_label('Difference (Degrees) [Calculated - Local]', rotation=270, labelpad=20)
    plt.title('Difference: Calculated VZA (from Lat/Lon) vs LocalZenithAngle\n(GOES-East 2km FD)', pad=15)
    plt.xlabel('Column Index')
    plt.ylabel('Row Index')
    
    plt.tight_layout()
    
    # FIX 2: Save the figure to your directory instead of trying to show it interactively
    save_path = r'F:\GOES\NAV\VZA_Difference_Map.png'
    plt.savefig(save_path, dpi=300)
    print(f"Success! Plot saved to: {save_path}")
    
    file_path = r'C:\noaa\goes\OR_ABI-L2-BRFF-M6_G19_s20250010500205_e20250010509513_c20250010513394.nc'
    with nc.Dataset(file_path, 'r') as f:
        # GOES-R variable 't' is seconds since J2000 epoch
        t_seconds = float(f.variables['t'][:])
        
    # Convert GOES-R epoch to a standard Python datetime
    goes_epoch = datetime.datetime(2000, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc)
    scan_time = goes_epoch + datetime.timedelta(seconds=t_seconds)
    
    print(f"File Scan Time: {scan_time}")

    # 2. Calculate the Solar Angles
    print("Calculating Solar Geometry...")
    sza, saa = calculate_solar_angles(lat_data, lon_data, scan_time)