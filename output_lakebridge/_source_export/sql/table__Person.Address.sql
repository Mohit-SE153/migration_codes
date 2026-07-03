CREATE TABLE [Person].[Address] (
    [AddressID] int NOT NULL,
    [AddressLine1] nvarchar(60) NOT NULL,
    [AddressLine2] nvarchar(60) NULL,
    [City] nvarchar(30) NOT NULL,
    [StateProvinceID] int NOT NULL,
    [PostalCode] nvarchar(15) NOT NULL,
    [SpatialLocation] geography NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
