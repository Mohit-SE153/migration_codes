CREATE TABLE [Person].[StateProvince] (
    [StateProvinceID] int NOT NULL,
    [StateProvinceCode] nchar(3) NOT NULL,
    [CountryRegionCode] nvarchar(3) NOT NULL,
    [IsOnlyStateProvinceFlag] bit NOT NULL,
    [Name] nvarchar(50) NOT NULL,
    [TerritoryID] int NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
